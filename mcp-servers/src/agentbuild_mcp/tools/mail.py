"""Mail (read-only on the server) and tasks: list, read, search, triage, reply drafts that are never sent."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from agentbuild.guardrails.input import sanitize
from agentbuild.mail.parser import MailMessage

from agentbuild_mcp.guards import UNTRUSTED_NOTE, one_line, untrusted
from agentbuild_mcp.runtime import Runtime, run_blocking
from agentbuild_mcp.tools._common import annotations, safe_id


def _headers(m: MailMessage) -> dict[str, Any]:
    sender = f"{m.sender_name} <{m.sender}>" if m.sender_name else m.sender
    return {"uid": m.uid, "from": one_line(sender), "subject": one_line(m.subject), "date": m.date,
            "attachments": [a.get("filename") for a in m.attachments]}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=annotations("List unread mail", read_only=True, open_world=True))
    async def mail_list_unread(ctx: Context, limit: int = 10) -> dict[str, Any]:
        """Unread messages (newest first): sender, subject, date and attachment names. Reading never marks mail
        as read. Use mail_read for a full message."""

        def work(rt: Runtime) -> dict[str, Any]:
            msgs = rt.mail.list_unread(max(1, min(limit, 100)))
            return {"messages": [_headers(m) | {"already_triaged": rt.db.is_processed(m.message_id)} for m in msgs],
                    "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work, label="reading the mailbox")

    @mcp.tool(annotations=annotations("Read one mail", read_only=True, open_world=True))
    async def mail_read(uid: str, ctx: Context) -> dict[str, Any]:
        """One message in full. The body is outside text: it is cleaned, wrapped as untrusted, and any phrases
        that look like instructions to an AI are listed in injection_flags."""
        safe_id(uid, "mail uid")

        def work(rt: Runtime) -> dict[str, Any]:
            m = rt.mail.get(uid)
            return _headers(m) | {"to": one_line(m.to), "message_id": m.message_id,
                                  "body": untrusted(m.body, "email", rt.cfg.max_text_chars),
                                  "injection_flags": sanitize(m.body).flags, "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work, label="reading the mailbox")

    @mcp.tool(annotations=annotations("Search mail by subject", read_only=True, open_world=True))
    async def mail_search(text: str, ctx: Context, since_days: int = 60, limit: int = 20) -> dict[str, Any]:
        """Messages (read or unread) whose subject contains `text`, e.g. a bug id like BUG-12345."""

        def work(rt: Runtime) -> dict[str, Any]:
            msgs = rt.mail.search_subject(text, since_days=max(1, since_days), limit=max(1, min(limit, 100)))
            return {"messages": [_headers(m) for m in msgs], "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work, label="searching the mailbox")

    @mcp.tool(annotations=annotations("Triage unread mail", open_world=True))
    async def mail_triage(ctx: Context, limit: int = 10, reprocess: bool = False) -> dict[str, Any]:
        """Let the local model triage unread mail: priority, category, summary, action items. Action items are
        saved as tasks and each mail is remembered as triaged (reprocess=true does it again). Release requests are
        only reported here; run QC explicitly with qc_run. Takes ~10-60 s per mail on a CPU."""

        def work(rt: Runtime) -> dict[str, Any]:
            rows = []
            for o in rt.sup.triage_inbox(max(1, min(limit, 50)), reprocess=reprocess, auto_qc=False):
                row = _headers(o.mail) | {"injection_flags": o.injection_flags}
                if o.triage is None:
                    rows.append(row | {"error": o.error})
                    continue
                t = o.triage
                rows.append(row | {"priority": t.priority.value, "category": t.category,
                                   "summary": one_line(t.summary, 1000),
                                   "action_items": [a.model_dump() for a in t.action_items],
                                   "needs_reply": t.needs_reply, "is_release_request": t.is_release_request,
                                   "repo": t.repo, "ref": t.ref, "suspicious": t.suspicious})
            return {"triaged": rows, "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work, label="the local model is triaging mail")

    @mcp.tool(annotations=annotations("Draft a reply (never sent)"))
    async def mail_draft_reply(uid: str, ctx: Context, instructions: str = "") -> dict[str, Any]:
        """Let the local model draft a reply to one message, following `instructions` (what the reply should say).
        The draft is saved as a local .eml file for the user to review; nothing is ever sent."""
        safe_id(uid, "mail uid")

        def work(rt: Runtime) -> dict[str, Any]:
            d, path = rt.sup.draft_reply(uid, instructions)
            return {"subject": d.subject, "body": d.body, "confidence": d.confidence, "saved_to": str(path),
                    "sent": False}

        return await run_blocking(ctx, work, label="the local model is drafting")

    @mcp.tool(annotations=annotations("List tasks", read_only=True))
    async def tasks_list(ctx: Context, status: str = "open") -> dict[str, Any]:
        """Tasks extracted from mail by mail_triage."""
        return await run_blocking(ctx, lambda rt: {"tasks": rt.db.list_tasks(status)})
