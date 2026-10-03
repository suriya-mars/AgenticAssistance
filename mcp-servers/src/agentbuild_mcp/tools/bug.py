"""The BUG quality process: one isolated context per bug id, run in rounds.

    scan → security mail → security approval → tester mail → tester approval → tester sign-off
         → PM + key leader mails → PM sign-off + key leader sign-off → COMPLETED
A failed ASoC gate or a security rejection blocks the bug; after fixing, bug_restart starts a new round.

Changes need the user's say-so: bug_draft / bug_mark_step / bug_reopen_step / bug_restart / bug_delete preview
with confirm=false; bug_sync returns `needs_decision` questions to answer with `decisions`.
"""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer

from agentbuild.bug.service import BugService, DraftResult, SyncItem
from agentbuild.bug.workflow import BY_KEY, DRAFTS, STEPS, missing_prereqs, next_steps

from agentbuild_mcp.approvals import DecisionApprover, FixedApprover, NeedsDecision
from agentbuild_mcp.guards import UNTRUSTED_NOTE, clip, one_line, resolve_document
from agentbuild_mcp.runtime import Runtime, run_blocking
from agentbuild_mcp.tools._common import CONFIRM_HINT, annotations

StepKey = Literal[tuple(s.key for s in STEPS)]  # type: ignore[valid-type]
DraftKind = Literal[tuple(DRAFTS)]               # type: ignore[valid-type]

_TOOL_FOR_STEP = {
    "scan_analyzed": "bug_scan(bug_id, report_path)",
    "security_mail": "bug_draft(bug_id, which='security')",
    "security_approval": "bug_sync(bug_id) once the security team has replied",
    "tester_mail": "bug_draft(bug_id, which='tester')",
    "tester_approval": "bug_sync(bug_id) once the tester has replied",
    "tester_signoff": "bug_sync(bug_id) once the tester has signed off",
    "signoff_mails": "bug_draft(bug_id, which='signoff')",
    "pm_signoff": "bug_sync(bug_id) once the PM has replied",
    "keyleader_signoff": "bug_sync(bug_id) once the key leader has replied",
}


def _next(svc: BugService, bug_id: str) -> list[dict[str, str]]:
    bug = svc.get(bug_id)
    if svc.blocked(bug):
        return [{"step": "restart", "label": "Fix the issue, then start a new round",
                 "tool": "bug_restart(bug_id, fix_note, confirm=true), then bug_scan(bug_id, report_path)"}]
    return [{"step": s.key, "label": s.label, "tool": _TOOL_FOR_STEP[s.key]} for s in next_steps(bug.done)]


def _scan(scan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not scan:
        return None
    keep = ("name", "application", "scan_name", "scan_date", "counts", "total", "gate", "gate_reason", "issues",
            "summary", "warnings", "injection_flags", "round")
    return {k: scan.get(k) for k in keep}


def _status(svc: BugService, bug_id: str) -> dict[str, Any]:
    st = svc.status(bug_id)
    bug = st["bug"]
    return {
        "bug_id": bug.bug_id, "title": bug.title, "round": st["round"], "complete": st["complete"],
        "blocked": st["blocked"], "steps": st["steps"], "next": _next(svc, bug.bug_id),
        "rejections_this_round": [e["detail"] for e in st["rejections"]],
        "scan": _scan(st["scan"]),
        "history": [{"round": h["round"], "blocked": h.get("blocked"), "fix_note": h.get("fix_note"),
                     "scan": h.get("scan")} for h in st["history"]],
        "thread_requested_by": sorted(st["thread_requested"]),
    }


def _draft(r: DraftResult, limit: int) -> dict[str, Any]:
    return {"template": r.template, "to": r.to, "subject": r.rendered.subject, "body": clip(r.rendered.body, limit),
            "attachments": r.attachments, "reply_on_existing_thread": r.in_thread,
            "unfilled_placeholders": r.rendered.missing, "saved": r.saved, "location": r.location}


def _item(it: SyncItem) -> dict[str, Any]:
    sender = f"{it.mail.sender_name} <{it.mail.sender}>" if it.mail.sender_name else it.mail.sender
    return {"from": one_line(sender), "subject": one_line(it.mail.subject), "date": it.mail.date,
            "role": it.role, "reads_as": it.kind, "summary": one_line(it.summary, 600), "action": it.action,
            "step": it.step, "note": one_line(it.note, 600)}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=annotations("BUG process overview", read_only=True))
    async def bug_process(ctx: Context) -> dict[str, Any]:
        """The steps of the BUG quality process, in order, and which tool completes each one."""
        return {"steps": [{"key": s.key, "label": s.label, "kind": s.kind, "requires": list(s.requires),
                           "completed_by": _TOOL_FOR_STEP[s.key]} for s in STEPS],
                "rules": ["Steps must be done in order; replies that arrive early are kept and applied later.",
                          "A failed ASoC security gate or a security-team rejection blocks the bug: fix it, then "
                          "bug_restart starts a new round with the same id (history is kept).",
                          "Only senders listed for a role (settings or bug_create) can approve or sign off.",
                          "Drafts are saved for the user to send; nothing is ever sent automatically."]}

    @mcp.tool(annotations=annotations("Start a bug's process"))
    async def bug_create(bug_id: str, title: str, ctx: Context, tester: list[str] | None = None,
                         pm: list[str] | None = None, keyleader: list[str] | None = None,
                         security: list[str] | None = None) -> dict[str, Any]:
        """Start the quality process for a bug. `bug_id` like BUG-12345 (or just 12345). Email lists override the
        default people for that role, for this bug only."""

        def work(rt: Runtime) -> dict[str, Any]:
            bug = rt.bugs.create(bug_id, title, {"tester": tester or [], "pm": pm or [],
                                                 "keyleader": keyleader or [], "security": security or []})
            return _status(rt.bugs, bug.bug_id)

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Change a bug's title or people", idempotent=True))
    async def bug_update(bug_id: str, ctx: Context, title: str | None = None, tester: list[str] | None = None,
                         pm: list[str] | None = None, keyleader: list[str] | None = None,
                         security: list[str] | None = None) -> dict[str, Any]:
        """Change the title, or who counts as tester / PM / key leader / security team for this bug."""

        def work(rt: Runtime) -> dict[str, Any]:
            bug = rt.bugs.update(bug_id, title, {"tester": tester or [], "pm": pm or [],
                                                 "keyleader": keyleader or [], "security": security or []})
            return _status(rt.bugs, bug.bug_id)

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("List bugs", read_only=True))
    async def bug_list(ctx: Context) -> dict[str, Any]:
        """All bugs with their round, progress, and whether they are blocked or complete."""

        def work(rt: Runtime) -> dict[str, Any]:
            rows = []
            for b in rt.bugs.store.list():
                st = rt.bugs.status(b.bug_id)
                rows.append({"bug_id": b.bug_id, "title": b.title, "round": st["round"], "complete": st["complete"],
                             "blocked": bool(st["blocked"]),
                             "progress": f"{sum(r['done'] for r in st['steps'])}/{len(st['steps'])}",
                             "next": [n["label"] for n in _next(rt.bugs, b.bug_id)]})
            return {"bugs": rows}

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Bug status", read_only=True))
    async def bug_status(bug_id: str, ctx: Context) -> dict[str, Any]:
        """Where a bug stands: steps done (who, when, conditions), blocked or not, scan result, earlier rounds,
        and the next tool to call."""
        return await run_blocking(ctx, lambda rt: _status(rt.bugs, bug_id))

    @mcp.tool(annotations=annotations("Analyse the ASoC report"))
    async def bug_scan(bug_id: str, report_path: str, ctx: Context) -> dict[str, Any]:
        """Analyse the bug's ASoC security report (a PDF in an allowed folder, e.g. files/asoc.pdf). Severity counts
        and the security gate are computed from the report text by code; the local model writes the summary.
        A failed gate blocks the bug. Takes ~1-6 minutes on a CPU."""

        def work(rt: Runtime) -> dict[str, Any]:
            rt.bugs.analyze_scan(bug_id, str(resolve_document(rt.cfg, report_path)))
            st = _status(rt.bugs, bug_id)
            return {"scan": st["scan"], "blocked": st["blocked"], "next": st["next"]}

        return await run_blocking(ctx, work, label="analysing the ASoC report")

    @mcp.tool(annotations=annotations("Draft the step's mail(s)"))
    async def bug_draft(bug_id: str, which: DraftKind, ctx: Context, confirm: bool = False, force: bool = False,
                        thread: bool | None = None) -> dict[str, Any]:
        """Fill the mail template(s) for a step: 'security', 'tester', or 'signoff' (PM + key leader).
        confirm=false shows exactly what would be saved; confirm=true saves the draft(s) to the Drafts folder
        or data/drafts. Nothing is ever sent. thread: true = reply on top of the existing mail thread with that
        person, false = new mail, null = automatic (re-submissions, or when they asked for it)."""

        def work(rt: Runtime) -> dict[str, Any]:
            results = rt.bugs.draft(bug_id, which, force=force, thread=thread)
            drafts = [_draft(r, rt.cfg.max_text_chars // 2) for r in results]
            if not confirm:
                return {"mode": "preview", "drafts": drafts, "would_save_to": rt.bugs.sink.label, "hint": CONFIRM_HINT}
            return {"mode": "saved", "drafts": drafts, "saved_to": rt.bugs.sink.label, "sent": False,
                    "next": _next(rt.bugs, bug_id)}

        return await run_blocking(ctx, work, approver=FixedApprover(confirm))

    @mcp.tool(annotations=annotations("Read the bug's replies", open_world=True))
    async def bug_sync(bug_id: str, ctx: Context,
                       decisions: dict[str, Literal["approve", "reject"]] | None = None) -> dict[str, Any]:
        """Read replies about this bug from the mailbox (subject contains the bug id) and apply approvals,
        sign-offs and rejections. Every step a reply would complete needs the user's answer: when status is
        'needs_decision', show the question and details to the user, then call bug_sync again with
        decisions={"<id>": "approve" or "reject"} (keep earlier answers in the dict). Repeat until status 'done'."""
        approver = DecisionApprover(decisions)

        def work(rt: Runtime) -> dict[str, Any]:
            items = rt.bugs.sync(bug_id)
            return {"status": "done", "replies": [_item(i) for i in items], "answered": approver.answered,
                    "bug": _status(rt.bugs, bug_id), "note": UNTRUSTED_NOTE}

        try:
            return await run_blocking(ctx, work, approver=approver, label="reading replies")
        except NeedsDecision as nd:
            return {"status": "needs_decision", "decision": nd.as_dict(), "answered_so_far": approver.answered,
                    "how_to_continue": "Show the question and details to the user. Then call bug_sync again with "
                                       f"decisions={{'{nd.decision_id}': 'approve' or 'reject'}} plus every "
                                       "earlier answer.",
                    "note": UNTRUSTED_NOTE}

    @mcp.tool(annotations=annotations("Ask about a bug", read_only=True))
    async def bug_ask(bug_id: str, question: str, ctx: Context) -> dict[str, Any]:
        """Answer a question about one bug with the local model, using only that bug's status, scan, mails and
        earlier rounds. `sources` lists the parts it used (status, scan, mail:N, scan-page:N)."""

        def work(rt: Runtime) -> dict[str, Any]:
            ans, unknown = rt.bugs.ask(bug_id, question)
            return {"answer": ans.answer, "sources": ans.sources, "cited_but_not_in_context": unknown}

        return await run_blocking(ctx, work, label="the local model is answering")

    @mcp.tool(annotations=annotations("Mark a step done by hand"))
    async def bug_mark_step(bug_id: str, step: StepKey, note: str, ctx: Context, confirm: bool = False,
                            force: bool = False) -> dict[str, Any]:
        """Mark a step done manually, e.g. a sign-off given in a meeting. `note` records why.
        force=true ignores step order and blocking. Preview with confirm=false first."""

        def work(rt: Runtime) -> dict[str, Any]:
            svc = rt.bugs
            if not confirm:
                bug = svc.get(bug_id)
                problems = []
                if not force and svc.blocked(bug):
                    problems.append(f"the bug is blocked: {svc.blocked(bug)['reason']}")
                if not force and (missing := missing_prereqs(step, bug.done)):
                    problems.append(f"needs first: {[BY_KEY[m].label for m in missing]}")
                return {"mode": "preview", "bug_id": bug.bug_id, "step": BY_KEY[step].label,
                        "already_done": step in bug.done, "note": note,
                        "would_fail": problems, "hint": CONFIRM_HINT}
            svc.mark(bug_id, step, note, force=force)
            return {"mode": "done", "bug": _status(svc, bug_id)}

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Undo a step", destructive=True))
    async def bug_reopen_step(bug_id: str, step: StepKey, reason: str, ctx: Context,
                              confirm: bool = False) -> dict[str, Any]:
        """Undo a step so it can be done again (the reason is recorded). Preview with confirm=false first."""

        def work(rt: Runtime) -> dict[str, Any]:
            svc = rt.bugs
            if not confirm:
                bug = svc.get(bug_id)
                return {"mode": "preview", "bug_id": bug.bug_id, "step": BY_KEY[step].label,
                        "currently_done": step in bug.done, "reason": reason, "hint": CONFIRM_HINT}
            svc.reopen(bug_id, step, reason)
            return {"mode": "done", "bug": _status(svc, bug_id)}

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Start a new round", destructive=True))
    async def bug_restart(bug_id: str, fix_note: str, ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """After fixing what blocked the bug (failed scan or security rejection): start a new round with the same
        id. This round's steps are cleared (kept in history) and replies dated before the restart stop counting.
        `fix_note` (what was fixed) goes into the next mails. Preview with confirm=false first."""

        def work(rt: Runtime) -> dict[str, Any]:
            svc = rt.bugs
            if not confirm:
                st = svc.status(bug_id)
                return {"mode": "preview", "bug_id": st["bug"].bug_id, "current_round": st["round"],
                        "new_round": st["round"] + 1, "blocked": st["blocked"],
                        "steps_that_will_be_cleared": [r["label"] for r in st["steps"] if r["done"]],
                        "fix_note": fix_note, "hint": CONFIRM_HINT}
            svc.restart(bug_id, fix_note)
            return {"mode": "done", "bug": _status(svc, bug_id)}

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Delete a bug", destructive=True))
    async def bug_delete(bug_id: str, ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Delete a bug with all its steps, history and linked mails (saved drafts are not touched).
        Cannot be undone. Preview with confirm=false first."""

        def work(rt: Runtime) -> dict[str, Any]:
            svc = rt.bugs
            bug = svc.get(bug_id)
            if not confirm:
                return {"mode": "preview", "bug_id": bug.bug_id, "title": bug.title, "round": svc.round(bug),
                        "linked_mails": len(svc.store.mails(bug.bug_id)), "hint": CONFIRM_HINT}
            return {"mode": "done", "deleted": svc.delete(bug.bug_id)}

        return await run_blocking(ctx, work)
