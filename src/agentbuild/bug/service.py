"""BUG quality process: one isolated context per bug id, run in rounds.

Who decides what:
  - code:  step order, who counts as security/tester/PM/key leader (sender address), severity counts,
           the security gate, blocking, and which replies belong to the current round
  - LLM:   reads the ASoC report, classifies replies, writes the scan summary, answers questions
  - you:   confirm every draft before it is saved and every step a reply would complete

A failed ASoC gate or a security-team rejection BLOCKS the bug. After fixing, `restart` starts a new round
with the same bug id: steps are cleared, the previous round is kept in history, and replies dated before
the restart no longer count.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from agentbuild.bug import asoc
from agentbuild.bug.store import Bug, BugStore
from agentbuild.bug.templates import Rendered, render
from agentbuild.bug.workflow import (BY_KEY, DRAFTS, STEPS, is_complete, mentions_bug, missing_prereqs,
                                     next_steps, normalize_bug_id, steps_for_reply)
from agentbuild.core.approval import Approver
from agentbuild.core.schemas import AsocAnalysis, BugAnswer, MailKind, MailSignal
from agentbuild.core.types import RunContext
from agentbuild.guardrails.input import sanitize, wrap_untrusted
from agentbuild.mail.drafts import DraftSink, build_message
from agentbuild.mail.parser import MailMessage
from agentbuild.storage.trace import Tracer

ROLES = ("security", "tester", "pm", "keyleader")
_DEFAULT_GATE = {"critical": 0, "high": 0, "medium": None, "low": None, "informational": None}


class WorkflowError(Exception):
    pass


@dataclass
class DraftResult:
    template: str
    role: str
    to: list[str]
    rendered: Rendered
    saved: bool
    location: str = ""
    attachments: list[str] = field(default_factory=list)
    in_thread: bool = False


@dataclass
class SyncItem:
    mail: MailMessage
    role: str | None
    kind: str
    summary: str
    # applied | dismissed | deferred | rejection | blocked_by_security | blocked | ignored_sender
    # | info | seen | previous_round
    action: str
    step: str = ""
    note: str = ""


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y") if ts else ""


def _mail_ts(date_header: str) -> float | None:
    try:
        return parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def _step_day(info: dict[str, Any] | None) -> str:
    """When a step happened: the reply mail's own Date header if it came from a mail, else when it was recorded."""
    if not info:
        return ""
    ts = _mail_ts(info.get("detail", {}).get("date") or "")
    return _day(ts or info.get("done_at"))


def _who(m: MailMessage) -> str:
    return f"{m.sender_name} <{m.sender}>" if m.sender_name else m.sender


def _strip_re(subject: str) -> str:
    while subject[:3].lower() in ("re:", "fw:") or subject[:4].lower() == "fwd:":
        subject = subject.split(":", 1)[1].strip()
    return subject


class BugService:
    def __init__(self, sup, store: BugStore, sink: DraftSink, approver: Approver):
        self.sup = sup
        self.s = sup.s
        self.cfg: dict[str, Any] = sup.s.raw.get("bug", {})
        self.store = store
        self.sink = sink
        self.approver = approver

    # ------------------------------------------------------------------ ids & people
    def bug_id(self, raw: str) -> str:
        return normalize_bug_id(raw, self.cfg.get("id_pattern", r"[A-Z][A-Z0-9_]*-\d+"),
                                self.cfg.get("id_prefix", "BUG-"))

    def get(self, raw: str) -> Bug:
        return self.store.get(self.bug_id(raw))

    def recipients(self, bug: Bug, role: str) -> tuple[str, list[str]]:
        default = self.cfg.get("recipients", {}).get(role, {})
        to = bug.data.get("recipients", {}).get(role) or default.get("to", [])
        return default.get("name", role.capitalize()), [a.strip().lower() for a in to]

    def role_of(self, bug: Bug, sender: str) -> str | None:
        """Exact address match only: approval authority is never inferred by the model or by domain."""
        s = sender.strip().lower()
        for role in ROLES:
            if s in self.recipients(bug, role)[1]:
                return role
        return None

    # ------------------------------------------------------------------ rounds & blocking
    @staticmethod
    def round(bug: Bug) -> int:
        return bug.data.get("round", 1)

    @staticmethod
    def blocked(bug: Bug) -> dict[str, Any] | None:
        if bug.data.get("blocked"):
            return bug.data["blocked"]
        scan = bug.data.get("scan") or {}
        if scan.get("gate") == "FAIL":   # also covers a crash between saving the scan and recording the block
            return {"by": "asoc_gate", "reason": f"ASoC scan failed: {scan.get('gate_reason')}",
                    "round": bug.data.get("round", 1)}
        return None

    def _block(self, bug: Bug, by: str, reason: str) -> None:
        bug.data["blocked"] = {"by": by, "reason": reason, "at": time.time(), "round": self.round(bug)}
        self.store.update(bug)
        self.store.event(bug.bug_id, "blocked", None, f"round {self.round(bug)} blocked by {by}: {reason}")

    def _require_not_blocked(self, bug: Bug) -> None:
        b = self.blocked(bug)
        if b:
            raise WorkflowError(
                f"{bug.bug_id} is BLOCKED ({b['by']}): {b['reason']}\n"
                f"Fix the issue, then start again from the scan with the same id:\n"
                f"  agent bug restart {bug.bug_id} --fix \"what you fixed\"\n"
                f"  agent bug scan {bug.bug_id} <new-asoc-report.pdf>")

    def restart(self, raw: str, fix_note: str) -> Bug:
        """Begin a new round with the same bug id. The finished/blocked round is archived in history."""
        bug = self.get(raw)
        now = time.time()
        bug.data.setdefault("history", []).append({
            "round": self.round(bug), "started": bug.data.get("round_started"), "ended": now,
            "steps": {k: {"done_at": v["done_at"], **v["detail"]} for k, v in bug.steps.items()},
            "scan": {k: bug.data.get("scan", {}).get(k) for k in ("name", "counts", "gate", "gate_reason")}
            if bug.data.get("scan") else None,
            "blocked": bug.data.get("blocked"), "fix_note": fix_note,
        })
        bug.data.update(round=self.round(bug) + 1, round_started=now, fix_note=fix_note, blocked=None, scan=None)
        self.store.update(bug)
        self.store.clear_steps(bug.bug_id)
        self.store.close_open_mails(bug.bug_id, "previous_round")
        self.store.event(bug.bug_id, "restarted", None, f"round {bug.data['round']} started. Fix: {fix_note}")
        return self.store.get(bug.bug_id)

    # ------------------------------------------------------------------ create / edit / delete
    def create(self, raw: str, title: str, overrides: dict[str, list[str]] | None = None) -> Bug:
        bug_id = self.bug_id(raw)
        if self.store.exists(bug_id):
            raise WorkflowError(f"{bug_id} already exists. See: agent bug status {bug_id}")
        data = {"recipients": {k: v for k, v in (overrides or {}).items() if v},
                "round": 1, "round_started": time.time(), "history": []}
        return self.store.create(bug_id, title, data)

    def update(self, raw: str, title: str | None = None, overrides: dict[str, list[str]] | None = None) -> Bug:
        bug = self.get(raw)
        if title:
            bug.title = title
        for role, addrs in (overrides or {}).items():
            if addrs:
                bug.data.setdefault("recipients", {})[role] = addrs
        self.store.update(bug)
        self.store.event(bug.bug_id, "updated", None, f"title={title} recipients={overrides}")
        return bug

    def delete(self, raw: str) -> str:
        bug = self.get(raw)
        self.store.delete(bug.bug_id)
        return bug.bug_id

    # ------------------------------------------------------------------ 1. ASoC scan
    def analyze_scan(self, raw: str, pdf_path: str) -> dict[str, Any]:
        bug = self.get(raw)
        self._require_not_blocked(bug)
        if "security_mail" in bug.done:
            raise WorkflowError(f"The security team already has this round's scan. To scan a new report, restart:\n"
                                f"  agent bug restart {bug.bug_id} --fix \"what changed\"")
        meta, _ = self.sup.ingest_pdf(pdf_path)
        pages = self.sup.docs.pages(meta.doc_id)
        budget = self.s.docs.get("context_chars", 12000)

        # The summary + issue-type overview live at the front of an ASoC report; add best-matching later pages.
        picked = [p for p in pages[:4] if p.text]
        used = sum(len(p.text) for p in picked)
        seen = {p.number for p in picked}
        for chunk, _ in self.sup.docs.search(meta.doc_id, "issue types severity high medium low count summary", k=8):
            if chunk.page not in seen and used + len(chunk.text) <= budget:
                picked.append(next(p for p in pages if p.number == chunk.page))
                seen.add(chunk.page)
                used += len(picked[-1].text)
        picked.sort(key=lambda p: p.number)
        body = "\n\n".join(f"[page {p.number}]\n{p.text}" for p in picked)
        san = sanitize(body, max_chars=budget)

        ctx = RunContext(trigger="cli", tainted=True, meta={"bug_id": bug.bug_id, "doc_id": meta.doc_id})
        task = (f"Bug {bug.bug_id}: {bug.title}\nASoC report: {meta.name} ({meta.pages} pages)\n\n"
                f"{wrap_untrusted(san.text, 'document')}")
        a: AsocAnalysis = self.sup._run("asoc_analyst", task, ctx)

        summary_text = "\n".join(p.text for p in pages[:3])
        counts, warnings = asoc.merge_counts({s: getattr(a, s) for s in asoc.SEVERITIES},
                                             asoc.regex_counts(summary_text))
        total_in_report = asoc.regex_total(summary_text)
        if total_in_report is not None and total_in_report != sum(counts.values()):
            warnings.append(f"severity counts add up to {sum(counts.values())}, report total says {total_in_report}")
        full_text = "\n".join(p.text for p in pages)
        llm_issues, dropped = asoc.verify_issues([i.model_dump() for i in a.top_issues], full_text)
        if dropped:
            warnings.append(f"dropped issue types not found in the report text: {dropped}")
        issues = asoc.merge_issues(llm_issues, asoc.regex_issue_table("\n".join(p.text for p in pages[:6])))[:10]
        meta_rx = asoc.regex_metadata("\n".join(p.text for p in pages[:2]))
        gate, reason = asoc.security_gate(counts, _DEFAULT_GATE | self.cfg.get("security_gate", {}))

        scan = {
            "doc_id": meta.doc_id, "file": meta.source, "name": meta.name, "pages": meta.pages,
            "application": meta_rx.get("application") or a.application,
            "scan_name": meta_rx.get("scan_name") or a.scan_name,
            "scan_date": meta_rx.get("scan_date") or a.scan_date,
            "counts": counts, "total": sum(counts.values()), "issues": issues, "summary": a.summary,
            "gate": gate, "gate_reason": reason, "warnings": warnings, "injection_flags": san.flags,
            "ocr_pages": meta.ocr_pages, "round": self.round(bug),
        }
        bug.data["scan"] = scan
        if gate == "FAIL":
            self._block(bug, "asoc_gate", f"ASoC scan failed: {reason}")   # saves the scan and the block together
        else:
            self.store.update(bug)
        self.store.complete(bug.bug_id, "scan_analyzed", {"file": meta.name, "gate": gate, "counts": counts})
        Tracer(self.s.path("runs_dir"), ctx.run_id).log("bug_scan", bug=bug.bug_id, gate=gate, warnings=warnings)
        return scan

    # ------------------------------------------------------------------ drafts
    def template_values(self, bug: Bug) -> dict[str, Any]:
        scan = bug.data.get("scan") or {}
        counts = scan.get("counts", {})
        st = bug.steps
        rnd = self.round(bug)

        def by(step: str) -> str:
            return st.get(step, {}).get("detail", {}).get("sender", "")

        findings = "\n".join(
            f"- {i['issue_type']} ({i['severity']}, {i.get('count', 1)})"
            + (f" - {i['location']}" if i.get("location") else "") for i in scan.get("issues", [])
        ) or ("- none listed" if scan else "")
        fix = bug.data.get("fix_note", "")
        v: dict[str, Any] = {
            "bug_id": bug.bug_id, "bug_title": bug.title, "my_name": self.cfg.get("my_name", ""),
            "today": datetime.now().strftime("%d %b %Y"), "round": rnd, "fix_note": fix,
            "resubmission_note": (f"This is re-submission #{rnd - 1} after fixing the earlier findings"
                                  + (f": {fix}." if fix else ".")) if rnd > 1 else "",
            "application": scan.get("application"), "scan_name": scan.get("scan_name") or scan.get("name"),
            "scan_date": scan.get("scan_date"),
            "severity_table": asoc.severity_table(counts) if counts else "",
            "severity_line": asoc.severity_line(counts) if counts else "",
            "total": scan.get("total"), "security_gate": scan.get("gate"),
            "security_gate_reason": scan.get("gate_reason"), "findings_list": findings,
            "scan_summary": scan.get("summary"),
            "security_mail_date": _day(st.get("security_mail", {}).get("done_at")),
            "security_approval_by": by("security_approval"),
            "security_approval_date": _step_day(st.get("security_approval")),
            "tester_mail_date": _day(st.get("tester_mail", {}).get("done_at")),
            "tester_approval_by": by("tester_approval"),
            "tester_approval_date": _step_day(st.get("tester_approval")),
            "tester_signoff_by": by("tester_signoff"),
            "tester_signoff_date": _step_day(st.get("tester_signoff")),
            "tester_conditions": st.get("tester_signoff", {}).get("detail", {}).get("conditions", ""),
        }
        v.update({s: counts.get(s) for s in asoc.SEVERITIES} if counts else {})
        return v

    def _thread(self, bug: Bug, role: str) -> dict[str, Any] | None:
        """The existing mail chain with this role (our mails to them and their replies), oldest first."""
        chain = [m for m in self.store.mails(bug.bug_id)
                 if m["role"] == role and (m["direction"] == "out" or m["status"] != "ignored_sender")]
        if not chain:
            return None
        first_out = next((m for m in chain if m["direction"] == "out"), chain[0])
        return {"subject": _strip_re(first_out["subject"]), "last": chain[-1],
                "references": [m["message_id"] for m in chain]}

    def _wants_thread(self, bug: Bug, role: str, thread: bool | None) -> bool:
        if thread is not None:
            return thread
        if bug.data.get("thread_requested", {}).get(role):
            return True
        return role == "security" and self.round(bug) > 1 and self.cfg.get("resubmit_in_same_thread", True)

    def draft(self, raw: str, which: str, force: bool = False, thread: bool | None = None) -> list[DraftResult]:
        """thread: True = reply on top of the existing mail chain with that role, False = new mail,
        None = automatic (security re-submissions, or when the recipient asked for it)."""
        if which not in DRAFTS:
            raise WorkflowError(f"unknown draft '{which}'. Use one of: {', '.join(DRAFTS)}")
        bug = self.get(raw)
        self._require_not_blocked(bug)
        step, parts = DRAFTS[which]
        missing = missing_prereqs(step, bug.done)
        if missing and not force:
            todo = "\n".join(f"  - {BY_KEY[m].label}: {BY_KEY[m].hint.format(bug_id=bug.bug_id)}" for m in missing)
            raise WorkflowError(f"Cannot draft the {which} mail yet. Finish first:\n{todo}\n(or use --force)")
        if step in bug.done and not force:
            raise WorkflowError(f"The {which} mail for {bug.bug_id} was already drafted. Use --force to draft it again.")

        tdir = self.s.root / self.cfg.get("templates_dir", "config/bug_templates")
        from_addr = self.s.mail.get("from_addr", "")
        already = {m["role"]: m for m in self.store.mails(bug.bug_id)
                   if m["direction"] == "out" and m["round"] == self.round(bug)}
        results = []
        for template, role in parts:
            if role in already and not force:   # saved before an interruption: don't create a duplicate draft
                prev = already[role]
                results.append(DraftResult(template, role, prev["recipients"].split(", "),
                                           Rendered(prev["subject"], prev["body"], []), True,
                                           location="already saved earlier in this round (not duplicated)"))
                continue
            name, to = self.recipients(bug, role)
            if not to:
                raise WorkflowError(f"No recipients for role '{role}'. Set bug.recipients.{role} in settings.yaml "
                                    f"or: agent bug set {bug.bug_id} --{role} someone@company.com")
            r = render(tdir / f"{template}.md", self.template_values(bug) | {"recipient_name": name})
            subject, body, in_reply_to, refs = r.subject, r.body, "", ""
            chain = self._thread(bug, role) if self._wants_thread(bug, role, thread) else None
            if chain:
                last = chain["last"]
                subject = f"Re: {chain['subject']}"
                quoted = "\n".join(f"> {line}" for line in (last["body"] or "").strip().splitlines()[:80])
                body = f"{r.body}\nOn {last['date']}, {last['sender']} wrote:\n{quoted}\n"
                in_reply_to = last["message_id"]
                refs = " ".join(chain["references"][:-1])
            attach = []
            scan_file = Path((bug.data.get("scan") or {}).get("file", ""))
            if role in self.cfg.get("attach_scan_to", []) and scan_file.is_file():
                attach.append(scan_file)
            msg = build_message(from_addr=from_addr, to=to, cc=self.cfg.get("cc") or [], subject=subject,
                                body=body, attachments=attach, in_reply_to=in_reply_to, references=refs)
            preview = (f"To: {', '.join(to)}\nSubject: {subject}\n"
                       + ("(reply on top of the existing mail thread)\n" if chain else "")
                       + (f"Attachments: {[a.name for a in attach]}\n" if attach else "") + f"\n{body}"
                       + (f"\n!! Unfilled placeholders: {r.missing}" if r.missing else ""))
            res = DraftResult(template, role, to, Rendered(subject, body, r.missing), False,
                              attachments=[a.name for a in attach], in_thread=chain is not None)
            if self.approver.approve(f"Save '{template}' draft for {bug.bug_id} to {self.sink.label}", preview):
                res.location = self.sink.save(msg, f"{bug.bug_id}_r{self.round(bug)}_{template}")
                res.saved = True
                self.store.add_mail(bug.bug_id, str(msg["Message-ID"]), direction="out", sender=from_addr,
                                    recipients=", ".join(to), subject=subject, date=str(msg["Date"]),
                                    body=body, role=role, kind="draft", status="drafted", round=self.round(bug))
                self.store.event(bug.bug_id, "draft_saved", step, f"{template} -> {res.location}")
                if chain:
                    bug.data.get("thread_requested", {}).pop(role, None)
                    self.store.update(bug)
            results.append(res)
        if results and all(r.saved for r in results):
            self.store.complete(bug.bug_id, step, {"drafts": [r.template for r in results]})
        return results

    # ------------------------------------------------------------------ replies
    def sync(self, raw: str) -> list[SyncItem]:
        bug = self.get(raw)
        if self.sup.mail is None:
            raise WorkflowError("no mail source configured")
        me = self.s.mail.get("from_addr", "").lower()
        round_start = bug.data.get("round_started") if self.round(bug) > 1 else None
        found = self.sup.mail.search_subject(bug.bug_id, self.cfg.get("mail_lookback_days", 60))
        items = []
        for m in found:
            if not mentions_bug(bug.bug_id, m.subject) or m.sender == me:
                continue
            role = self.role_of(bug, m.sender)
            row = next((x for x in self.store.mails(bug.bug_id) if x["message_id"] == m.message_id), None)
            if row and row["status"] not in ("new", ""):
                items.append(SyncItem(m, row["role"], row["kind"], row["summary"],
                                      "previous_round" if row["status"] == "previous_round" else "seen"))
                continue
            sent = _mail_ts(m.date)
            old = round_start is not None and sent is not None and sent < round_start
            if row:
                kind, summary, conditions, changes, in_thread = row["kind"], row["summary"], "", "", False
            else:
                block, flags = self.sup._mail_block(m)
                sig: MailSignal = self.sup._run(
                    "bug_mail_classifier",
                    f"Bug: {bug.bug_id} ({bug.title})\nSender role: "
                    f"{role or 'unknown - not a listed security team, tester, PM or key leader'}\n\n{block}",
                    RunContext(trigger="mail", tainted=True, meta={"bug_id": bug.bug_id}))
                kind, summary, conditions = sig.kind.value, sig.summary, sig.conditions
                changes, in_thread = sig.requested_changes, sig.reply_in_thread
                if flags:
                    summary += " [suspicious content flagged]"
                self.store.add_mail(bug.bug_id, m.message_id, uid=m.uid, direction="in",
                                    sender=_who(m), subject=m.subject, date=m.date,
                                    body=m.body, role=role, kind=kind,
                                    summary=summary + (f" Conditions: {conditions}" if conditions else "")
                                    + (f" Requested: {changes}" if changes else ""),
                                    status="previous_round" if old else "new",
                                    round=self.round(bug) - 1 if old else self.round(bug))
            if old:
                items.append(SyncItem(m, role, kind, summary, "previous_round",
                                      note="dated before this round started; does not count for this round"))
                continue
            if in_thread and role:
                bug.data.setdefault("thread_requested", {})[role] = True
                self.store.update(bug)
                self.store.event(bug.bug_id, "thread_requested", None, f"{role} asked for replies on the same thread")
            items.append(self._apply(bug, m, role, kind, summary, conditions, changes))
            bug = self.store.get(bug.bug_id)
        return items

    def _apply(self, bug: Bug, m: MailMessage, role: str | None, kind: str, summary: str,
               conditions: str, changes: str = "") -> SyncItem:
        if kind in (MailKind.APPROVAL.value, MailKind.SIGNOFF.value) and role is None:
            self.store.set_mail_status(bug.bug_id, m.message_id, "ignored_sender")
            self.store.event(bug.bug_id, "ignored_sender", None, f"{kind} from unlisted sender {m.sender}")
            return SyncItem(m, role, kind, summary, "ignored_sender",
                            note=f"{m.sender} is not listed for any role on {bug.bug_id}; not counted")
        if kind == MailKind.REJECTION.value:
            blocked_now = False
            if role == "security" and not self.blocked(bug):
                detail = (f"From: {_who(m)}\nDate: {m.date}\nSubject: {m.subject}\n"
                          f"Reads as: rejection - {summary}" + (f"\nRequested: {changes}" if changes else ""))
                # Ask before recording anything: if interrupted here, the next sync asks again.
                if self.approver.approve(f"Security team rejected {bug.bug_id}. Block the process?", detail):
                    self._block(bug, "security_team", changes or summary)
                    blocked_now = True
            self.store.event(bug.bug_id, "rejection", None, f"{role or m.sender}: {summary}")
            self.store.set_mail_status(bug.bug_id, m.message_id, "rejection")
            if blocked_now:
                return SyncItem(m, role, kind, summary, "blocked_by_security",
                                note=f"fix, then: agent bug restart {bug.bug_id} --fix \"...\"")
            return SyncItem(m, role, kind, summary, "rejection", note=changes)
        if self.blocked(bug):
            # Keep it 'new': after the restart it is older than the new round and will be set aside.
            return SyncItem(m, role, kind, summary, "blocked", note="process is blocked; not applied")
        todo = [s for s in steps_for_reply(role, kind) if s not in bug.done]
        if not todo:
            self.store.set_mail_status(bug.bug_id, m.message_id, "info")
            return SyncItem(m, role, kind, summary, "info", note=changes)

        done = set(bug.done)
        last = None
        for step in todo:
            missing = missing_prereqs(step, done)
            if missing:
                # Keep the mail 'new' so a later sync applies it once the earlier steps are done.
                return SyncItem(m, role, kind, summary, "deferred", step,
                                note=f"waiting for: {', '.join(BY_KEY[x].label for x in missing)}")
            detail = (f"From: {_who(m)} ({role})\nDate: {m.date}\nSubject: {m.subject}\n"
                      f"Reads as: {kind} - {summary}" + (f"\nConditions: {conditions}" if conditions else ""))
            if not self.approver.approve(f"Mark '{BY_KEY[step].label}' for {bug.bug_id}?", detail):
                self.store.set_mail_status(bug.bug_id, m.message_id, "dismissed")
                self.store.event(bug.bug_id, "dismissed", step, f"mail from {m.sender} not accepted")
                return SyncItem(m, role, kind, summary, "dismissed", step)
            self.store.complete(bug.bug_id, step, {"sender": _who(m),
                                                   "date": m.date, "message_id": m.message_id,
                                                   "conditions": conditions})
            done.add(step)
            last = step
        self.store.set_mail_status(bug.bug_id, m.message_id, "applied")
        return SyncItem(m, role, kind, summary, "applied", last or "")

    # ------------------------------------------------------------------ manual override
    def mark(self, raw: str, step: str, note: str = "", force: bool = False) -> None:
        bug = self.get(raw)
        if step not in BY_KEY:
            raise WorkflowError(f"unknown step '{step}'. Steps: {', '.join(BY_KEY)}")
        if not force:
            self._require_not_blocked(bug)
        missing = missing_prereqs(step, bug.done)
        if missing and not force:
            raise WorkflowError(f"'{step}' needs {missing} first (or use --force)")
        self.store.complete(bug.bug_id, step, {"manual": True, "note": note})

    def reopen(self, raw: str, step: str, reason: str) -> None:
        bug = self.get(raw)
        self.store.reopen(bug.bug_id, step, reason)

    # ------------------------------------------------------------------ status
    def status(self, raw: str) -> dict[str, Any]:
        bug = self.get(raw)
        rows = []
        for s in STEPS:
            info = bug.steps.get(s.key)
            d = (info or {}).get("detail", {})
            who = d.get("sender") or ("manual" + (f": {d['note']}" if d.get("note") else "") if d.get("manual") else "")
            rows.append({"key": s.key, "label": s.label, "done": info is not None,
                         "when": _step_day(info), "by": who, "conditions": d.get("conditions", "")})
        b = self.blocked(bug)
        nxt = ([{"label": "Fix the issue and restart",
                 "hint": f"agent bug restart {bug.bug_id} --fix \"what you fixed\"  then  agent bug scan "
                         f"{bug.bug_id} <new-report.pdf>"}] if b else
               [{"label": s.label, "hint": s.hint.format(bug_id=bug.bug_id)} for s in next_steps(bug.done)])
        rejections = [e for e in self.store.events(bug.bug_id) if e["kind"] == "rejection"
                      and e["ts"] >= bug.data.get("round_started", 0)]
        return {"bug": bug, "round": self.round(bug), "blocked": b, "steps": rows, "next": nxt,
                "complete": is_complete(bug.done) and not b, "rejections": rejections,
                "scan": bug.data.get("scan"), "history": bug.data.get("history", []),
                "thread_requested": bug.data.get("thread_requested", {})}

    # ------------------------------------------------------------------ Q&A
    def build_context(self, bug: Bug, question: str) -> tuple[str, set[str]]:
        budget = self.cfg.get("context_chars", 12000)
        st = self.status(bug.bug_id)
        signoffs = {"security_approval": "security team (approval)", "tester_signoff": "tester",
                    "pm_signoff": "project manager", "keyleader_signoff": "key leader"}
        got = [f"{who} ({r['by']}, {r['when']})" for r in st["steps"] for k, who in signoffs.items()
               if r["key"] == k and r["done"]]
        pending = [who for k, who in signoffs.items() if k not in bug.done]
        b = st["blocked"]
        lines = [f"[status]\nBug {bug.bug_id}: {bug.title}\nCurrent round: {st['round']}",
                 f"Process complete: {'YES' if st['complete'] else 'NO'}",
                 f"BLOCKED: YES - by {b['by']}: {b['reason']}" if b else "BLOCKED: no",
                 "Required approvals (only these four): security team, tester, project manager, key leader.",
                 f"Approvals RECEIVED this round: {'; '.join(got) or 'none'}",
                 f"Approvals PENDING this round: {', '.join(pending) or 'none - all received'}",
                 "Steps this round:"]
        for r in st["steps"]:
            lines.append(f"- {r['label']}: " + (f"done {r['when']}" + (f" by {r['by']}" if r['by'] else "")
                                                 + (f" (conditions: {r['conditions']})" if r['conditions'] else "")
                                                 if r["done"] else "not done"))
        lines += [f"- NEXT: {n['label']}" for n in st["next"]]
        lines += [f"- REJECTION: {e['detail']}" for e in st["rejections"]]
        for h in st["history"]:
            why = f"blocked by {h['blocked']['by']}: {h['blocked']['reason']}" if h.get("blocked") else "restarted"
            scan = f"; scan {h['scan']['gate']} ({asoc.severity_line(h['scan']['counts'])})" if h.get("scan") else ""
            lines.append(f"- Earlier round {h['round']}: {why}{scan}; fixed: {h.get('fix_note') or '-'}")
        blocks = ["\n".join(lines)]
        ids = {"status"}
        scan = bug.data.get("scan")
        if scan:
            issues = "\n".join(f"  - {i['issue_type']} ({i['severity']}, {i.get('count', 1)})" for i in scan["issues"])
            blocks.append(f"[scan]\nRound {scan.get('round', st['round'])} report: {scan['name']} ({scan['pages']} pages), "
                          f"application {scan['application']}, scan date {scan['scan_date']}\n"
                          f"Counts: {asoc.severity_line(scan['counts'])}\n"
                          f"Security gate: {scan['gate']} ({scan['gate_reason']})\nIssues:\n{issues}\n"
                          f"Summary: {scan['summary']}")
            ids.add("scan")
        used = sum(len(b) for b in blocks)
        mails = self.store.mails(bug.bug_id)
        for n, x in reversed(list(enumerate(mails, start=1))):   # newest first within budget
            body = sanitize(x["body"] or "", max_chars=1500).text
            block = (f"[mail:{n}] round {x['round']} - {'SENT by us' if x['direction'] == 'out' else 'RECEIVED'} "
                     f"{x['date']}\nFrom: {x['sender']} ({x['role'] or 'unlisted'})  To: {x['recipients']}\n"
                     f"Subject: {x['subject']}\nClassified as: {x['kind']}\n{wrap_untrusted(body, 'email')}")
            if used + len(block) > budget:
                break
            blocks.append(block)
            ids.add(f"mail:{n}")
            used += len(block)
        if scan:
            for chunk, _ in self.sup.docs.search(scan["doc_id"], question, k=3):
                block = f"[scan-page:{chunk.page}]\n{wrap_untrusted(sanitize(chunk.text).text, 'document')}"
                if used + len(block) > budget:
                    break
                blocks.append(block)
                ids.add(f"scan-page:{chunk.page}")
                used += len(block)
        return "\n\n".join(blocks), ids

    def ask(self, raw: str, question: str, history: list[tuple[str, str]] | None = None) -> tuple[BugAnswer, list[str]]:
        bug = self.get(raw)
        context, ids = self.build_context(bug, question)
        convo = "".join(f"Q: {q}\nA: {a}\n" for q, a in (history or [])[-3:])
        task = (f"{context}\n\n" + (f"Earlier in this conversation:\n{convo}\n" if convo else "")
                + f"Question (from the user, trusted) about {bug.bug_id}: {question}")
        ans: BugAnswer = self.sup._run("bug_qa", task, RunContext(trigger="cli", tainted=True,
                                                                   meta={"bug_id": bug.bug_id}))
        unknown = [s for s in ans.sources if s not in ids]
        ans.sources = [s for s in ans.sources if s in ids]
        return ans, unknown
