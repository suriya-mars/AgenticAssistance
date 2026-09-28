"""CLI entry point: `agent ...`."""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from agentbuild.bug.service import BugService, WorkflowError
from agentbuild.bug.store import BugStore
from agentbuild.config import Settings, load_settings
from agentbuild.core.approval import CLIApprover
from agentbuild.core.schemas import Intent
from agentbuild.core.supervisor import Supervisor
from agentbuild.docs.extract import PdfError, ocr_problem
from agentbuild.docs.qa import QAResult
from agentbuild.guardrails.sandbox import SandboxError
from agentbuild.llm.ollama_client import OllamaClient, OllamaUnavailable
from agentbuild.mail.drafts import IMAPDraftSink, LocalDraftSink
from agentbuild.mail.imap_source import IMAPSource, LocalEmlSource, MailSource
from agentbuild.storage.db import DB
from agentbuild.tools.qc.loader import load_qc_tools
from agentbuild.tools.qc.report import to_markdown

app = typer.Typer(help="Local multi-agent assistant (Ollama): mail triage + release QC.", no_args_is_help=True)
mail_app = typer.Typer(help="Mail agents (read-only; drafts are never sent).", no_args_is_help=True)
qc_app = typer.Typer(help="Release quality checks.", no_args_is_help=True)
runs_app = typer.Typer(help="Run history and audit traces.", no_args_is_help=True)
bug_app = typer.Typer(help="BUG quality process: ASoC scan -> security -> tester -> PM + key leader sign-off.",
                      no_args_is_help=True)
pdf_app = typer.Typer(help="Ask questions about PDF documents (answers cite and verify pages).", no_args_is_help=True)
app.add_typer(mail_app, name="mail")
app.add_typer(pdf_app, name="pdf")
app.add_typer(bug_app, name="bug")
app.add_typer(qc_app, name="qc")
app.add_typer(runs_app, name="runs")
console = Console()


def _mail_source(s: Settings) -> MailSource:
    m = s.mail
    if m.get("source") == "imap":
        i = m["imap"]
        return IMAPSource(i["host"], i["user"], i.get("password_env", "AGENT_IMAP_PASSWORD"),
                          i.get("folder", "INBOX"), i.get("port", 993))
    d = Path(m.get("local_dir", "samples/mail"))
    return LocalEmlSource(d if d.is_absolute() else s.root / d)


def _build() -> tuple[Settings, Supervisor, DB]:
    s = load_settings()
    o = s.ollama
    llm = OllamaClient(o.get("url", "http://localhost:11434"), o.get("num_ctx", 8192),
                       o.get("temperature", 0.2), o.get("keep_alive", "5m"),
                       timeout_s=o.get("timeout_s", 300) * s.timeout_scale, think=o.get("think", False))
    db = DB(s.path("db"))
    sup = Supervisor(s, llm, CLIApprover(console), db, _mail_source(s), load_qc_tools(s.qc_tools_file()))
    return s, sup, db


# ---------------------------------------------------------------------- doctor
@app.command()
def doctor():
    """Check Ollama, models, mail access and QC tool binaries."""
    s = load_settings()
    ok = lambda b: "[green]OK[/]" if b else "[red]FAIL[/]"  # noqa: E731
    console.print(f"Project root: {s.root}")
    console.print(f"Models: fast={s.models.get('fast')} main={s.models.get('main')} "
                  f"quality={s.models.get('quality', s.models.get('main'))} | "
                  f"thinking={'on' if s.ollama.get('think') else 'off'} | timeout scale x{s.timeout_scale:g}")
    client = OllamaClient(s.ollama.get("url", "http://localhost:11434"))
    try:
        have = client.list_models()
        console.print(f"{ok(True)} Ollama reachable ({len(have)} models)")
        for role, tag in s.models.items():
            present = any(h == tag or h.startswith(tag + ":") or h == tag + ":latest" for h in have)
            console.print(f"  {ok(present)} {role}: {tag}" + ("" if present else f"  -> ollama pull {tag}"))
    except Exception as e:
        console.print(f"{ok(False)} Ollama not reachable at {client.base_url}: {e}")
    src = _mail_source(s)
    try:
        if isinstance(src, IMAPSource):
            src.check_login()
            console.print(f"{ok(True)} IMAP login (read-only) {src.user}@{src.host}")
        else:
            n = len(src.list_unread(1000))
            console.print(f"{ok(True)} Local mail dir {src.dir} ({n} .eml files)")
    except Exception as e:
        console.print(f"{ok(False)} Mail source: {e}")
    for t in load_qc_tools(s.qc_tools_file()):
        console.print(f"  {ok(t.available())} qc tool '{t.name}' ({t.cfg.argv[0]}) "
                      f"({t.severity.value}{', required' if t.required else ', optional'})")
    console.print(f"Allowed repo roots: {[str(p) for p in s.allowed_repo_roots()]}")
    problem = ocr_problem(s.docs.get("ocr_lang", "eng"))
    console.print(f"{ok(problem is None)} PDF OCR ({s.docs.get('ocr_lang', 'eng')})" + (f": {problem}" if problem else ""))


# ------------------------------------------------------------------------ mail
@mail_app.command("triage")
def mail_triage(limit: int = 10, reprocess: bool = typer.Option(False, help="Re-triage already processed mail"),
                auto_qc: bool = typer.Option(True, help="Offer QC runs for release-request mails")):
    """Summarise, prioritise and extract tasks from unread mail."""
    _, sup, _ = _build()
    outcomes = sup.triage_inbox(limit, reprocess=reprocess, auto_qc=auto_qc)
    if not outcomes:
        console.print("No new mail.")
        return
    t = Table(title="Inbox triage", show_lines=True)
    for c in ("uid", "prio", "cat", "from", "subject / summary", "tasks", "flags"):
        t.add_column(c)
    for o in outcomes:
        if o.triage is None:
            t.add_row(o.mail.uid, "-", "-", o.mail.sender, o.mail.subject, "", f"[red]error: {o.error[:80]}")
            continue
        tr = o.triage
        flags = []
        if tr.suspicious:
            flags.append("[red]suspicious[/]")
        if tr.needs_reply:
            flags.append("reply")
        if tr.is_release_request:
            flags.append(f"release:{o.qc_report.verdict.value if o.qc_report else 'not run'}")
        t.add_row(o.mail.uid, tr.priority.value, tr.category, o.mail.sender,
                  f"[bold]{o.mail.subject}[/]\n{tr.summary}", "\n".join(a.title for a in tr.action_items),
                  " ".join(flags))
    console.print(t)


@mail_app.command("draft")
def mail_draft(uid: str, instructions: str = typer.Option("", "--say", help="What the reply should say")):
    """Draft a reply to a mail (saved locally as .eml, never sent)."""
    _, sup, _ = _build()
    d, path = sup.draft_reply(uid, instructions)
    console.print(f"[bold]{d.subject}[/]  (confidence {d.confidence:.2f})\n\n{d.body}\n")
    console.print(f"Saved draft: {path}")


@app.command()
def tasks():
    """List open tasks extracted from mail."""
    _, _, db = _build()
    t = Table(title="Open tasks")
    for c in ("id", "title", "due", "owner"):
        t.add_column(c)
    for r in db.list_tasks():
        t.add_row(str(r["id"]), r["title"], r["due"] or "", r["owner"] or "")
    console.print(t)


# -------------------------------------------------------------------------- qc
@qc_app.command("run")
def qc_run(repo: str, ref: str = typer.Option(None, help="Branch/tag/commit (checked out in a temp worktree)"),
           no_llm: bool = typer.Option(False, help="Skip LLM analysis; deterministic checks only")):
    """Run the release QC pipeline on a repository."""
    _, sup, _ = _build()
    try:
        rep = sup.run_qc(repo, ref, trigger="cli", use_llm=not no_llm)
    except SandboxError as e:
        console.print(f"[red]Refused:[/] {e}")
        raise typer.Exit(2)
    console.print(Markdown(to_markdown(rep)))
    console.print(f"Report: {sup.last_report_path}")
    raise typer.Exit(0 if rep.verdict.value == "GO" else 1)


@qc_app.command("tools")
def qc_tools():
    """List configured QC plug-ins."""
    s = load_settings()
    for t in load_qc_tools(s.qc_tools_file()):
        console.print(f"- {t.name} ({t.severity.value}, {'required' if t.required else 'optional'}) "
                      f"{'installed' if t.available() else 'MISSING'}: {' '.join(t.cfg.argv)}")


# ------------------------------------------------------------------------- pdf
_MARK = {"exact": "[green]verified[/]", "page_corrected": "[yellow]verified, page corrected[/]",
         "approximate": "[yellow]close match[/]", "unverified": "[red]NOT FOUND in document[/]"}


def _doc(sup: Supervisor, ref: str):
    try:
        return sup.resolve_doc(ref)
    except (PdfError, KeyError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2)


def _show_answer(r: QAResult) -> None:
    console.print(Markdown(r.answer))
    for c in r.citations:
        note = f" (model said p.{c.claimed_page})" if c.claimed_page else ""
        console.print(f"  [dim]p.{c.page}[/] {_MARK[c.status]}{note}: [italic]\u201c{c.quote}\u201d[/]")
    if r.found and not r.grounded:
        console.print("  [red]No quote could be matched to the document. Check this answer yourself.[/]")
    if r.context:
        where = "full text" if r.context.mode == "full" else f"passages from pages {r.context.pages}"
        console.print(f"  [dim]read: {where}[/]")


@pdf_app.command("add")
def pdf_add(path: Path, force: bool = typer.Option(False, help="Re-extract even if already added")):
    """Extract and index a PDF (OCR for scanned pages)."""
    _, sup, _ = _build()
    try:
        meta, new = sup.ingest_pdf(str(path), force=force)
    except PdfError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2)
    ocr = f", OCR on pages {meta.ocr_pages}" if meta.ocr_pages else ""
    console.print(f"{'Added' if new else 'Already added'}: [bold]{meta.name}[/] id={meta.doc_id} "
                  f"({meta.pages} pages, {meta.chunks} passages, {meta.chars:,} chars{ocr})")
    if meta.empty_pages:
        s = load_settings()
        why = ocr_problem(s.docs.get("ocr_lang", "eng")) if s.docs.get("ocr", True) else "OCR disabled"
        console.print(f"[yellow]No text on pages {meta.empty_pages}[/] (scanned images?)"
                      + (f"; {why}. Then re-run with --force." if why else "; OCR found nothing either."))


@pdf_app.command("list")
def pdf_list():
    """List added documents."""
    _, sup, _ = _build()
    t = Table(title="Documents")
    for c in ("id", "name", "pages", "chars", "ocr pages"):
        t.add_column(c)
    for m in sup.docs.list():
        t.add_row(m.doc_id, m.name, str(m.pages), f"{m.chars:,}", str(len(m.ocr_pages)))
    console.print(t)


@pdf_app.command("ask")
def pdf_ask(doc: str = typer.Argument(..., help="PDF path, document id, or part of its name"),
            question: str = typer.Argument(...)):
    """Ask one question about a PDF."""
    _, sup, _ = _build()
    _show_answer(sup.ask_pdf(_doc(sup, doc), question))


@pdf_app.command("chat")
def pdf_chat(doc: str = typer.Argument(..., help="PDF path, document id, or part of its name")):
    """Interactive Q&A about a PDF. Follow-up questions keep context. Type 'exit' to quit."""
    _, sup, _ = _build()
    meta = _doc(sup, doc)
    console.print(f"[bold]{meta.name}[/] - {meta.pages} pages. Ask a question (exit to quit).")
    history: list[tuple[str, str]] = []
    while True:
        try:
            q = console.input("\n[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit", ":q"}:
            break
        if not q:
            continue
        try:
            r = sup.ask_pdf(meta, q, history)
        except Exception as e:
            console.print(f"[red]error: {e}[/]")
            continue
        _show_answer(r)
        history.append((q, r.answer))


@pdf_app.command("remove")
def pdf_remove(doc: str):
    """Remove a document's extracted text and index."""
    _, sup, _ = _build()
    m = _doc(sup, doc)
    sup.docs.remove(m.doc_id)
    console.print(f"Removed {m.name} ({m.doc_id})")


# ------------------------------------------------------------------------- bug
def _bugs() -> BugService:
    s, sup, db = _build()
    drafts = s.path("drafts_dir") / "bugs"
    if s.mail.get("draft_target") == "imap" and isinstance(sup.mail, IMAPSource):
        sink = IMAPDraftSink(sup.mail, s.mail.get("drafts_folder", "Drafts"), drafts)
    else:
        sink = LocalDraftSink(drafts)
    return BugService(sup, BugStore(db.conn), sink, sup.approver)


def _bug_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except (WorkflowError, KeyError, ValueError, PdfError) as e:
        console.print(f"[red]{escape(str(e).strip(chr(39)))}[/]")
        raise typer.Exit(2)


_PEOPLE = {
    "tester": typer.Option(None, "--tester", help="Tester email (repeatable); overrides settings"),
    "pm": typer.Option(None, "--pm", help="Project manager email (repeatable)"),
    "keyleader": typer.Option(None, "--keyleader", help="Key leader email (repeatable)"),
    "security": typer.Option(None, "--security", help="Security team email (repeatable)"),
}


@bug_app.command("new")
def bug_new(bug_id: str, title: str = typer.Option(..., "--title", "-t", help="Short description of the bug"),
            tester: list[str] = _PEOPLE["tester"], pm: list[str] = _PEOPLE["pm"],
            keyleader: list[str] = _PEOPLE["keyleader"], security: list[str] = _PEOPLE["security"]):
    """Start the quality process for a bug (its id is the unique key for everything after)."""
    svc = _bugs()
    bug = _bug_call(svc.create, bug_id, title,
                    {"tester": tester, "pm": pm, "keyleader": keyleader, "security": security})
    console.print(f"Created [bold]{bug.bug_id}[/]: {escape(bug.title)}\nNext: agent bug scan {bug.bug_id} <asoc-report.pdf>")


@bug_app.command("set")
def bug_set(bug_id: str, title: str = typer.Option(None, "--title", "-t"),
            tester: list[str] = _PEOPLE["tester"], pm: list[str] = _PEOPLE["pm"],
            keyleader: list[str] = _PEOPLE["keyleader"], security: list[str] = _PEOPLE["security"]):
    """Change a bug's title or the people for a role."""
    svc = _bugs()
    bug = _bug_call(svc.update, bug_id, title, {"tester": tester, "pm": pm, "keyleader": keyleader,
                                                 "security": security})
    console.print(f"Updated {bug.bug_id}")


@bug_app.command("list")
def bug_list():
    """All bugs and where each one is."""
    svc = _bugs()
    t = Table(title="Bugs")
    for c in ("bug", "title", "progress", "next"):
        t.add_column(c)
    for b in svc.store.list():
        st = svc.status(b.bug_id)
        done = sum(r["done"] for r in st["steps"])
        nxt = "COMPLETED" if st["complete"] else ", ".join(n["label"] for n in st["next"])
        t.add_row(b.bug_id, escape(b.title), f"{done}/{len(st['steps'])}", escape(nxt))
    console.print(t)


@bug_app.command("status")
def bug_status(bug_id: str):
    """Timeline, scan result and the next step for one bug."""
    svc = _bugs()
    st = _bug_call(svc.status, bug_id)
    bug = st["bug"]
    console.print(f"[bold]{bug.bug_id}[/] - {escape(bug.title)}  (round {st['round']})"
                  + ("  [bold green]COMPLETED[/]" if st["complete"] else ""))
    for h in st["history"]:
        why = f"blocked by {h['blocked']['by']}: {h['blocked']['reason']}" if h.get("blocked") else "restarted"
        console.print(f"  [dim]round {h['round']}: {escape(why)} -> fixed: {escape(h.get('fix_note') or '-')}[/]")
    if st["blocked"]:
        console.print(f"[bold red]BLOCKED[/] by {st['blocked']['by']}: {escape(st['blocked']['reason'])}")
    for role in st["thread_requested"]:
        console.print(f"  [cyan]{role} asked for the next mail as a reply on the same thread[/]")
    if st["scan"]:
        sc = st["scan"]
        color = "green" if sc["gate"] == "PASS" else "red"
        console.print(f"ASoC: {escape(sc['name'])} | {sc['counts']} | gate [{color}]{sc['gate']}[/] "
                      f"({escape(sc['gate_reason'])})")
    for r in st["steps"]:
        mark = "[green]done[/]" if r["done"] else "[dim]....[/]"
        extra = f" {r['when']}" + (f" by {escape(r['by'])}" if r["by"] else "") if r["done"] else ""
        cond = f" [yellow](conditions: {escape(r['conditions'])})[/]" if r["conditions"] else ""
        console.print(f"  {mark}  {r['label']}{extra}{cond}")
    for e in st["rejections"]:
        console.print(f"  [red]REJECTION:[/] {escape(e['detail'])}")
    for n in st["next"]:
        console.print(f"[bold cyan]Next:[/] {n['label']}  ->  {escape(n['hint'])}")


@bug_app.command("scan")
def bug_scan(bug_id: str, pdf: Path = typer.Argument(..., help="ASoC scan report PDF")):
    """Analyze the ASoC scan report PDF for this bug."""
    svc = _bugs()
    sc = _bug_call(svc.analyze_scan, bug_id, str(pdf))
    color = "green" if sc["gate"] == "PASS" else "red"
    console.print(f"[bold]{escape(sc['name'])}[/] ({sc['pages']} pages) - application: {escape(sc['application'] or '?')}, "
                  f"scan date: {escape(sc['scan_date'] or '?')}")
    t = Table(show_header=True)
    for c in ("critical", "high", "medium", "low", "informational", "total"):
        t.add_column(c, justify="right")
    t.add_row(*[str(sc["counts"][k]) for k in ("critical", "high", "medium", "low", "informational")], str(sc["total"]))
    console.print(t)
    console.print(f"Security gate: [{color}]{sc['gate']}[/] - {escape(sc['gate_reason'])}")
    for i in sc["issues"]:
        console.print(f"  - {escape(i['issue_type'])} ({i['severity']}, {i.get('count', 1)})"
                      + (f" - {escape(i['location'])}" if i.get("location") else ""))
    console.print(f"\n{escape(sc['summary'])}")
    for w in sc["warnings"]:
        console.print(f"[yellow]check:[/] {escape(w)}")
    if sc["injection_flags"]:
        console.print("[red]The report contains text that looks like instructions to an AI; it was ignored.[/]")
    bid = bug_id.upper() if not bug_id.isdigit() else svc.bug_id(bug_id)
    if sc["gate"] == "FAIL":
        console.print(f"\n[bold red]BLOCKED:[/] the ASoC scan found security issues above the allowed limits.\n"
                      f"Fix them, run a new scan, then start this bug again from the beginning:\n"
                      f"  agent bug restart {bid} --fix \"what you fixed\"\n"
                      f"  agent bug scan {bid} <new-asoc-report.pdf>")
    else:
        console.print(f"\nNext: agent bug draft {bid} security   (the security team must approve even a passing scan)")


@bug_app.command("draft")
def bug_draft(bug_id: str, which: str = typer.Argument(..., help="security | tester | signoff (PM + key leader)"),
              force: bool = typer.Option(False, help="Draft even if earlier steps are not done / redraft"),
              thread: bool = typer.Option(None, "--thread/--new-thread",
                                          help="Reply on top of the existing mail thread, or start a new mail "
                                               "(default: automatic for re-submissions / when asked)")):
    """Fill the mail template(s) for this step and save to Drafts after you confirm."""
    svc = _bugs()
    for r in _bug_call(svc.draft, bug_id, which, force=force, thread=thread):
        if r.saved:
            how = " (as a reply on the existing thread)" if r.in_thread else ""
            console.print(f"[green]Saved[/] {r.template} draft{how} to {escape(r.location)}")
        else:
            console.print(f"[yellow]Not saved:[/] {r.template}")
        if r.rendered.missing:
            console.print(f"  [yellow]unfilled placeholders:[/] {r.rendered.missing}")


@bug_app.command("sync")
def bug_sync(bug_id: str):
    """Read this bug's replies from the inbox and confirm approvals / sign-offs."""
    svc = _bugs()
    items = _bug_call(svc.sync, bug_id)
    if not items:
        console.print(f"No mails with {bug_id.upper()} in the subject yet.")
    style = {"applied": "green", "dismissed": "yellow", "deferred": "cyan", "rejection": "red",
             "blocked_by_security": "bold red", "blocked": "yellow", "ignored_sender": "red", "info": "dim",
             "seen": "dim", "previous_round": "dim"}
    for it in items:
        c = style.get(it.action, "white")
        console.print(f"[{c}]{it.action:<14}[/] {escape(it.mail.sender)} ({it.role or 'unlisted'}) - {it.kind}: "
                      f"{escape(it.summary)}" + (f" -> {it.step}" if it.step else "")
                      + (f"\n{'':15}{escape(it.note)}" if it.note else ""))
    st = svc.status(bug_id)
    if st["blocked"]:
        console.print(f"[bold red]BLOCKED[/] by {st['blocked']['by']}: {escape(st['blocked']['reason'])}")
    console.print("[bold green]All approvals and sign-offs received - COMPLETED[/]" if st["complete"] else
                  "Next: " + ", ".join(escape(n["hint"]) for n in st["next"]))


@bug_app.command("mark")
def bug_mark(bug_id: str, step: str, note: str = typer.Option("", help="Why / evidence"),
             force: bool = typer.Option(False, help="Ignore step order")):
    """Manually mark a step done (e.g. sign-off given verbally)."""
    svc = _bugs()
    _bug_call(svc.mark, bug_id, step, note, force)
    console.print(f"Marked {step} done for {bug_id.upper()}")


@bug_app.command("restart")
def bug_restart(bug_id: str, fix: str = typer.Option(..., "--fix", help="What was fixed (goes into the next mails)")):
    """Start the process again from the scan, same bug id (after fixing ASoC issues or a security rejection)."""
    svc = _bugs()
    bug = _bug_call(svc.restart, bug_id, fix)
    console.print(f"{bug.bug_id} is now in round {bug.data['round']}. Earlier rounds are kept in its history.\n"
                  f"Next: agent bug scan {bug.bug_id} <new-asoc-report.pdf>")


@bug_app.command("delete")
def bug_delete(bug_id: str, yes: bool = typer.Option(False, "--yes", help="Skip the confirmation")):
    """Delete a bug and all its steps, timeline and linked mails (drafts already saved are not touched)."""
    svc = _bugs()
    bug = _bug_call(svc.get, bug_id)
    if not yes and not svc.approver.approve(f"Delete {bug.bug_id} and all its history?", escape(bug.title)):
        raise typer.Exit(1)
    console.print(f"Deleted {svc.delete(bug.bug_id)}")


@bug_app.command("reopen")
def bug_reopen(bug_id: str, step: str, reason: str = typer.Option(..., help="Why it is reopened")):
    """Undo a step (e.g. after a rejection), so it can be done again."""
    svc = _bugs()
    _bug_call(svc.reopen, bug_id, step, reason)
    console.print(f"Reopened {step} for {bug_id.upper()}")


def _show_bug_answer(ans, unknown) -> None:
    console.print(Markdown(ans.answer))
    if ans.sources:
        console.print(f"  [dim]sources: {', '.join(ans.sources)}[/]")
    if unknown:
        console.print(f"  [yellow]model cited sources that were not in its context: {unknown}[/]")


@bug_app.command("ask")
def bug_ask(bug_id: str, question: str):
    """Ask about one bug; answers use only that bug's scan, mails and timeline."""
    svc = _bugs()
    _show_bug_answer(*_bug_call(svc.ask, bug_id, question))


@bug_app.command("chat")
def bug_chat(bug_id: str):
    """Interactive Q&A about one bug. Type 'exit' to quit."""
    svc = _bugs()
    bug = _bug_call(svc.get, bug_id)
    console.print(f"[bold]{bug.bug_id}[/] - {escape(bug.title)}. Ask anything about this bug (exit to quit).")
    history: list[tuple[str, str]] = []
    while True:
        try:
            q = console.input("\n[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit", ":q"}:
            break
        if q:
            ans, unknown = svc.ask(bug.bug_id, q, history)
            _show_bug_answer(ans, unknown)
            history.append((q, ans.answer))


# ------------------------------------------------------------------------ runs
@runs_app.command("list")
def runs_list(limit: int = 20):
    _, _, db = _build()
    t = Table(title="Recent runs")
    for c in ("run_id", "agent", "trigger", "status"):
        t.add_column(c)
    for r in db.recent_runs(limit):
        t.add_row(r["run_id"], r["agent"], r["trigger"], r["status"])
    console.print(t)


@runs_app.command("show")
def runs_show(run_id: str):
    """Print the audit trace of a run."""
    s = load_settings()
    p = s.path("runs_dir") / f"{run_id}.jsonl"
    if not p.exists():
        raise typer.BadParameter(f"no trace {p}")
    for line in p.read_text().splitlines():
        rec = json.loads(line)
        ev = rec.pop("event")
        rec.pop("ts", None)
        console.print(f"[cyan]{ev}[/] {json.dumps(rec, ensure_ascii=False)[:400]}")


# ------------------------------------------------------------------------- ask
@app.command()
def ask(request: str):
    """Free-text request; the supervisor routes it to the right agent."""
    _, sup, _ = _build()
    r = sup.route(request)
    console.print(f"[dim]route: {r.intent.value} {r.target} {r.ref} ({r.reason})[/]")
    if r.intent is Intent.TRIAGE:
        mail_triage(limit=10, reprocess=False, auto_qc=True)
    elif r.intent is Intent.DRAFT and r.target:
        mail_draft(r.target, "")
    elif r.intent is Intent.QC and r.target:
        qc_run(r.target, r.ref or None, no_llm=False)
    elif r.intent is Intent.TASKS:
        tasks()
    elif r.intent is Intent.BUG and r.target:
        drafts = {"draft_security": "security", "draft_tester": "tester", "draft_signoff": "signoff"}
        if r.action in drafts:
            bug_draft(r.target, drafts[r.action], force=False)
        elif r.action == "sync":
            bug_sync(r.target)
        elif r.action == "ask" or (r.question and r.action != "status"):
            bug_ask(r.target, r.question or request)
        else:
            bug_status(r.target)
    elif r.intent is Intent.PDF and r.target:
        pdf_ask(r.target, r.question or request)
    else:
        console.print("I can triage mail, draft replies, run QC, list tasks, answer questions about a PDF, or run a bug's quality process.")


def main() -> None:
    """Entry point: clear one-line errors for problems the user can fix, instead of a traceback."""
    try:
        app()
    except OllamaUnavailable as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
