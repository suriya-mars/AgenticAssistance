"""Supervisor: builds agents from config, routes requests, and owns the cross-agent handoffs.

Handoffs are deterministic code, not LLM decisions:
  mail -> triage -> (release request?) -> allowlist gate -> human approval -> QC with a CLEAN context
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agentbuild.config import Settings
from agentbuild.core.agent import Agent, AgentSpec
from agentbuild.core.approval import Approver
from agentbuild.core.schemas import OUTPUT_MODELS, DraftReply, PdfAnswer, QCAnalysis, RouteDecision, TriageResult
from agentbuild.core.types import AgentError, Budget, RunContext
from agentbuild.docs.extract import extract_pages
from agentbuild.docs.qa import QAResult, build_context, check_citations
from agentbuild.docs.store import DocMeta, DocStore
from agentbuild.guardrails import sandbox
from agentbuild.guardrails.input import sanitize, wrap_untrusted
from agentbuild.llm.ollama_client import LLM
from agentbuild.llm.router import ModelRouter
from agentbuild.mail.drafts import save_draft
from agentbuild.mail.imap_source import MailSource
from agentbuild.mail.parser import MailMessage
from agentbuild.storage.db import DB
from agentbuild.storage.trace import Tracer
from agentbuild.tools.qc.base import QCTool
from agentbuild.tools.qc.loader import register_qc_tools, workspace
from agentbuild.tools.qc.report import QCReport, compute_verdict, to_markdown
from agentbuild.tools.registry import ToolRegistry


@dataclass
class TriageOutcome:
    mail: MailMessage
    triage: TriageResult | None
    injection_flags: list[str]
    error: str = ""
    qc_report: QCReport | None = None


class Supervisor:
    def __init__(self, settings: Settings, llm: LLM, approver: Approver, db: DB,
                 mail_source: MailSource | None, qc_tools: list[QCTool]):
        self.s = settings
        self.llm = llm
        self.approver = approver
        self.db = db
        self.mail = mail_source
        self.qc_tools = qc_tools
        self.router = ModelRouter(settings.models)
        self.registry = ToolRegistry()
        register_qc_tools(self.registry, qc_tools)
        self.docs = DocStore(settings.path("docs_dir"))

    # ------------------------------------------------------------ agent factory
    def agent(self, name: str, ctx: RunContext) -> tuple[Agent, Tracer]:
        cfg = self.s.agents[name]
        spec = AgentSpec(
            name=name,
            model=cfg["model"],
            system_prompt=self.s.prompt(cfg["prompt"]),
            output_model=OUTPUT_MODELS[cfg["output"]],
            tools=cfg.get("tools", []),
            budget=Budget(**(cfg.get("budget", {}) | {"timeout_s": cfg.get("budget", {}).get("timeout_s", 300.0)
                                                           * self.s.timeout_scale})),
        )
        tracer = Tracer(self.s.path("runs_dir"), ctx.run_id)
        return Agent(spec, self.llm, self.router, self.registry, self.s.policy, self.approver, tracer), tracer

    def _run(self, name: str, task: str, ctx: RunContext):
        agent, tracer = self.agent(name, ctx)
        try:
            out = agent.run(task, ctx)
            self.db.record_run(ctx.run_id, name, ctx.trigger, "ok", out.model_dump(), ctx.started)
            return out
        except Exception as e:
            tracer.log("agent_failed", agent=name, error=repr(e))
            self.db.record_run(ctx.run_id, name, ctx.trigger, "error", {"error": repr(e)}, ctx.started)
            raise

    # ------------------------------------------------------------------ routing
    def route(self, text: str) -> RouteDecision:
        return self._run("supervisor", text, RunContext(trigger="cli"))

    # --------------------------------------------------------------------- mail
    def _mail_block(self, m: MailMessage) -> tuple[str, list[str]]:
        san = sanitize(m.body, max_chars=self.s.mail.get("max_body_chars", 6000))
        subj = sanitize(m.subject, max_chars=300)
        header = (f"From: {m.sender_name} <{m.sender}>\nTo: {m.to}\nDate: {m.date}\n"
                  f"Subject: {subj.text}\nAttachments: {[a['filename'] for a in m.attachments]}\n\n")
        return wrap_untrusted(header + san.text, "email"), san.flags + subj.flags

    def triage_inbox(self, limit: int = 10, reprocess: bool = False, auto_qc: bool = True) -> list[TriageOutcome]:
        assert self.mail is not None, "no mail source configured"
        outcomes = []
        for m in self.mail.list_unread(limit):
            if not reprocess and self.db.is_processed(m.message_id):
                continue
            outcomes.append(self.triage_one(m, auto_qc=auto_qc))
        return outcomes

    def triage_one(self, m: MailMessage, auto_qc: bool = True) -> TriageOutcome:
        ctx = RunContext(trigger="mail", tainted=True, meta={"uid": m.uid})
        block, flags = self._mail_block(m)
        if flags:
            Tracer(self.s.path("runs_dir"), ctx.run_id).log("injection_flags", uid=m.uid, flags=flags)
        try:
            tri: TriageResult = self._run("mail_triage", f"Triage this email:\n\n{block}", ctx)
        except AgentError as e:
            return TriageOutcome(m, None, flags, error=str(e))
        if flags:
            tri.suspicious = True
        for item in tri.action_items:
            self.db.add_task(item.title, m.message_id, item.due, item.owner)
        self.db.mark_processed(m.message_id, m.uid, m.sender, m.subject, tri.model_dump())
        out = TriageOutcome(m, tri, flags)
        if auto_qc and tri.is_release_request:
            out.qc_report = self.handle_release_request(m, tri, ctx.run_id)
        return out

    def draft_reply(self, uid: str, instructions: str = "") -> tuple[DraftReply, Path]:
        assert self.mail is not None, "no mail source configured"
        m = self.mail.get(uid)
        block, _ = self._mail_block(m)
        task = f"Draft a reply to this email.\n\n{block}"
        if instructions:
            task += f"\n\nInstructions from the user (trusted): {instructions}"
        d: DraftReply = self._run("reply_drafter", task, RunContext(trigger="mail", tainted=True))
        path = save_draft(m, d.subject, d.body, self.s.path("drafts_dir"), self.s.mail.get("from_addr", ""))
        return d, path

    # ---------------------------------------------------------------------- PDF
    def ingest_pdf(self, path: str, force: bool = False) -> tuple[DocMeta, bool]:
        """Extract + index a PDF. Returns (meta, newly_added). Same bytes => same doc id (dedupe)."""
        if not force and (existing := self.docs.exists_for(Path(path))):
            return existing, False
        d = self.s.docs
        pages = extract_pages(Path(path), ocr=d.get("ocr", True), ocr_lang=d.get("ocr_lang", "eng"),
                              max_pages=d.get("max_pages", 500), max_file_mb=d.get("max_file_mb", 50))
        return self.docs.add(Path(path), pages, chunk_size=d.get("chunk_chars", 900)), True

    def resolve_doc(self, ref: str) -> DocMeta:
        """A doc id / name fragment from the store, or a path to a PDF (ingested on first use)."""
        p = Path(ref).expanduser()
        if p.suffix.lower() == ".pdf" and p.is_file():
            return self.ingest_pdf(str(p))[0]
        return self.docs.resolve(ref)

    def ask_pdf(self, doc: DocMeta, question: str, history: list[tuple[str, str]] | None = None) -> QAResult:
        history = history or []
        d = self.s.docs
        ctx_doc = build_context(self.docs, doc, question, history[-1][0] if history else "",
                                budget_chars=d.get("context_chars", 12000), top_k=d.get("top_k", 6))
        if not ctx_doc.text:
            return QAResult("I couldn't find anything related to that question in the document.", False,
                            [], ctx_doc)
        san = sanitize(ctx_doc.text, max_chars=d.get("context_chars", 12000) + 2000)
        convo = "".join(f"Q: {q}\nA: {a}\n" for q, a in history[-3:])
        task = (f"Document: {doc.name} ({doc.pages} pages). "
                f"{'Full text' if ctx_doc.mode == 'full' else 'Most relevant excerpts'} below.\n\n"
                f"{wrap_untrusted(san.text, 'document')}\n\n"
                + (f"Earlier in this conversation:\n{convo}\n" if convo else "")
                + f"Question (from the user, trusted): {question}")
        run_ctx = RunContext(trigger="cli", tainted=True, meta={"doc_id": doc.doc_id})
        ans: PdfAnswer = self._run("pdf_qa", task, run_ctx)
        checked = check_citations([(c.page, c.quote) for c in ans.citations], self.docs.pages(doc.doc_id))
        Tracer(self.s.path("runs_dir"), run_ctx.run_id).log(
            "pdf_citations", doc=doc.doc_id, mode=ctx_doc.mode, pages=ctx_doc.pages,
            citations=[(c.page, c.status) for c in checked], injection_flags=san.flags)
        return QAResult(ans.answer, ans.found, checked, ctx_doc)

    # ----------------------------------------------------------------- handoff
    def handle_release_request(self, m: MailMessage, tri: TriageResult, parent_run: str) -> QCReport | None:
        tracer = Tracer(self.s.path("runs_dir"), parent_run)
        if not self.s.policy.sender_may_trigger_qc(m.sender):
            tracer.log("qc_trigger_denied", sender=m.sender, reason="sender not in allowlist")
            return None
        repo = self._resolve_repo_name(tri.repo)
        if repo is None:
            tracer.log("qc_trigger_denied", repo=tri.repo, reason="repo not found under allowed roots")
            return None
        detail = f"From: {m.sender}\nSubject: {m.subject}\nRepo: {repo}\nRef: {tri.ref or 'HEAD'}"
        approved = self.approver.approve("Start QC run requested by email", detail)
        self.db.record_approval(parent_run, "qc_from_mail", detail, approved)
        tracer.log("approval", action="qc_from_mail", approved=approved)
        if not approved:
            return None
        # Clean context: only repo + ref cross the boundary; the email body never reaches the QC agent.
        return self.run_qc(str(repo), tri.ref or None, trigger="mail")

    def _resolve_repo_name(self, name: str) -> Path | None:
        if not name:
            return None
        for root in self.s.allowed_repo_roots():
            for cand in (Path(name).expanduser(), root.expanduser() / name):
                try:
                    return sandbox.resolve_repo(str(cand), self.s.allowed_repo_roots())
                except sandbox.SandboxError:
                    continue
        return None

    # ----------------------------------------------------------------------- QC
    def run_qc(self, repo: str, ref: str | None = None, trigger: str = "cli", use_llm: bool = True) -> QCReport:
        repo_path = sandbox.resolve_repo(repo, self.s.allowed_repo_roots())
        ctx = RunContext(trigger=trigger)
        tracer = Tracer(self.s.path("runs_dir"), ctx.run_id)
        tracer.log("qc_start", repo=str(repo_path), ref=ref, trigger=trigger)

        with workspace(repo_path, ref) as ws:
            ctx.meta.update(workspace=str(ws), ref=ref, qc_results={})
            # 1) Deterministic: every required check runs, no LLM involved.
            for t in self.qc_tools:
                if t.required:
                    res = t.run(ws, ref)
                    ctx.meta["qc_results"][t.name] = res
                    tracer.log("qc_check", name=t.name, status=res.status.value, summary=res.summary)

            # 2) Advisory: LLM explains results; may run optional checks / read files (guardrailed).
            analysis, recs = "", []
            if use_llm:
                digest = [{"name": r.name, "status": r.status.value, "severity": r.severity.value,
                           "summary": r.summary, "findings": r.findings[:10]}
                          for r in ctx.meta["qc_results"].values()]
                optional = [t.name for t in self.qc_tools if not t.required]
                task = (f"Repository: {repo_path.name} @ {ref or 'HEAD'}\n"
                        f"Required check results:\n{json.dumps(digest, indent=1, default=str)}\n"
                        f"Optional checks you may run: {optional or 'none'}")
                try:
                    a: QCAnalysis = self._run("qc", task, ctx)
                    analysis, recs = a.analysis, a.recommendations
                except Exception as e:
                    analysis = f"(LLM analysis unavailable: {e})"

        # 3) Verdict is recomputed from ALL results, including optional checks the agent ran.
        results = list(ctx.meta["qc_results"].values())
        verdict, blockers = compute_verdict(results)
        report = QCReport(repo=str(repo_path), ref=ref, verdict=verdict, blockers=blockers,
                          checks=results, analysis=analysis, recommendations=recs)
        out_dir = self.s.path("reports_dir")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"qc_{ctx.run_id}.md").write_text(to_markdown(report))
        (out_dir / f"qc_{ctx.run_id}.json").write_text(report.model_dump_json(indent=2))
        tracer.log("qc_done", verdict=verdict.value, blockers=blockers)
        self.db.record_run(ctx.run_id, "qc_pipeline", trigger, verdict.value, report.model_dump(), ctx.started)
        self.last_report_path = out_dir / f"qc_{ctx.run_id}.md"
        return report
