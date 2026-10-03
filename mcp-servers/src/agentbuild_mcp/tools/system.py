"""Health check and the audit trail."""
from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentbuild.docs.extract import ocr_problem
from agentbuild.mail.imap_source import IMAPSource

from agentbuild_mcp.guards import UNTRUSTED_NOTE, clip
from agentbuild_mcp.runtime import Runtime, run_blocking
from agentbuild_mcp.tools._common import annotations


def _installed(tag: str, have: list[str]) -> bool:
    return any(h == tag or h.startswith(tag + ":") or h == tag + ":latest" for h in have)


def register_health(mcp: MCPServer) -> None:
    @mcp.tool(annotations=annotations("Health check", read_only=True, open_world=True))
    async def system_health(ctx: Context) -> dict[str, Any]:
        """Check that AgentBuild is ready: Ollama and its models, the mail source, QC checks and OCR.
        Call this first when another tool fails."""

        def work(rt: Runtime) -> dict[str, Any]:
            s = rt.s
            out: dict[str, Any] = {"agentbuild_home": str(rt.cfg.home), "models": dict(s.models),
                                   "timeout_scale": s.timeout_scale}
            try:
                have = rt.llm.list_models()
                out["ollama"] = {"reachable": True, "url": s.ollama.get("url"), "installed": have,
                                 "missing": sorted({m for m in s.models.values() if not _installed(m, have)})}
            except Exception as e:
                out["ollama"] = {"reachable": False, "url": s.ollama.get("url"), "error": str(e)}
            try:
                if isinstance(rt.mail, IMAPSource):
                    rt.mail.check_login()
                    out["mail"] = {"source": "imap", "host": rt.mail.host, "user": rt.mail.user, "login": "ok"}
                else:
                    out["mail"] = {"source": "local", "folder": str(rt.mail.dir),
                                   "messages": len(rt.mail.list_unread(1000))}
            except Exception as e:
                out["mail"] = {"source": s.mail.get("source", "local"), "error": str(e)}
            out["qc_checks"] = [{"name": t.name, "available": t.available(), "severity": t.severity.value,
                                 "required": t.required} for t in rt.sup.qc_tools]
            out["ocr"] = ocr_problem(s.docs.get("ocr_lang", "eng")) or "ok"
            out["allowed_doc_roots"] = [str(r) for r in rt.cfg.doc_roots]
            out["allowed_repo_roots"] = [str(r) for r in s.allowed_repo_roots()]
            return out

        return await run_blocking(ctx, work, label="health check")


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=annotations("Recent runs", read_only=True))
    async def runs_list(ctx: Context, limit: int = 20) -> dict[str, Any]:
        """List recent agent runs (newest first). Each run_id can be opened with run_trace."""

        def work(rt: Runtime) -> dict[str, Any]:
            runs = rt.db.recent_runs(max(1, min(limit, 200)))
            return {"runs": [{k: r[k] for k in ("run_id", "agent", "trigger", "status", "started_at", "finished_at")}
                             for r in runs]}

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Audit trace of a run", read_only=True))
    async def run_trace(run_id: str, ctx: Context, max_events: int = 200) -> dict[str, Any]:
        """Every step of one run: model replies, tool calls, policy decisions, approvals. Secrets are redacted,
        but the trace can contain email and document text."""
        if not re.fullmatch(r"[0-9a-f]{6,32}", run_id):
            raise ToolError(f"invalid run_id: {run_id!r} (use an id from runs_list)")

        def work(rt: Runtime) -> dict[str, Any]:
            path = rt.s.path("runs_dir") / f"{run_id}.jsonl"
            if not path.is_file():
                raise ToolError(f"no trace for run {run_id}")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            events = []
            for line in lines[: max(1, min(max_events, 1000))]:
                try:
                    events.append(json.loads(line) if len(line) <= 4000 else {"truncated": clip(line, 4000)})
                except json.JSONDecodeError:
                    events.append({"unparsed": clip(line, 500)})
            return {"run_id": run_id, "events": events, "total_events": len(lines), "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work)
