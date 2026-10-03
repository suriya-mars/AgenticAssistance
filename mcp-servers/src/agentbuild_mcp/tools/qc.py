"""Release quality checks: list the configured checks, run one or the whole pipeline, read a repository file.

Checks run real programs (tests, linters) on a repository, so they preview first (confirm=false) and only touch
repositories under qc.allowed_repo_roots in ../config/settings.yaml. A run on `ref` uses a temporary git worktree:
the working copy is never changed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentbuild.guardrails import sandbox
from agentbuild.tools.qc.loader import workspace

from agentbuild_mcp.approvals import FixedApprover
from agentbuild_mcp.guards import UNTRUSTED_NOTE, clip, untrusted_verbatim
from agentbuild_mcp.runtime import Runtime, run_blocking
from agentbuild_mcp.tools._common import CONFIRM_HINT, annotations


def _repo(rt: Runtime, repo: str) -> Path:
    """Path relative to the AgentBuild folder (e.g. repos/my-service); must be under an allowed repo root."""
    return sandbox.resolve_repo(str(rt.path(repo)), rt.s.allowed_repo_roots())


def _check(t: Any, repo: Path | None = None, ref: str | None = None) -> dict[str, Any]:
    cfg = getattr(t, "cfg", None)
    command = (t.build_argv(repo, ref) if repo is not None else cfg.argv) if cfg is not None else None
    return {"name": t.name, "description": t.description, "severity": t.severity.value, "required": t.required,
            "available": t.available() if hasattr(t, "available") else True, "command": command}


def _result(r: Any, limit: int) -> dict[str, Any]:
    d = r.model_dump(mode="json")
    d["log_tail"] = clip(d.get("log_tail") or "", limit)
    d["findings"] = d.get("findings", [])[:50]
    return d


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=annotations("List QC checks", read_only=True))
    async def qc_list_checks(ctx: Context) -> dict[str, Any]:
        """The configured quality checks (config/qc_tools.yaml): severity, whether the pipeline always runs them,
        whether the program is installed, and the exact command."""
        return await run_blocking(ctx, lambda rt: {"checks": [_check(t) for t in rt.sup.qc_tools]})

    @mcp.tool(annotations=annotations("Run one QC check"))
    async def qc_run_check(repo: str, check: str, ctx: Context, ref: str | None = None,
                           confirm: bool = False) -> dict[str, Any]:
        """Run a single check (see qc_list_checks) on a repository, e.g. repo='repos/my-service', check='tests'.
        confirm=false shows the exact command without running it."""

        def work(rt: Runtime) -> dict[str, Any]:
            repo_path = _repo(rt, repo)
            tool = next((t for t in rt.sup.qc_tools if t.name == check), None)
            if tool is None:
                raise ToolError(f"unknown check '{check}'. Checks: {[t.name for t in rt.sup.qc_tools]}")
            if not confirm:
                return {"mode": "preview", "repo": str(repo_path), "ref": ref or "working copy",
                        "check": _check(tool, repo_path, ref), "hint": CONFIRM_HINT}
            with workspace(repo_path, ref) as ws:
                return {"mode": "done", "repo": str(repo_path), "ref": ref or "working copy",
                        "result": _result(tool.run(ws, ref), rt.cfg.max_text_chars // 2)}

        return await run_blocking(ctx, work, label=f"running {check}")

    @mcp.tool(annotations=annotations("Run the QC pipeline"))
    async def qc_run(repo: str, ctx: Context, ref: str | None = None, use_llm: bool = True,
                     confirm: bool = False) -> dict[str, Any]:
        """Run every required check on a repository and return GO / NO_GO / NEEDS_REVIEW. The verdict comes from
        fixed rules (a failed blocker = NO_GO); with use_llm the local model adds an explanation and may run
        optional checks. confirm=false lists what would run."""

        def work(rt: Runtime) -> dict[str, Any]:
            repo_path = _repo(rt, repo)
            if not confirm:
                return {"mode": "preview", "repo": str(repo_path), "ref": ref or "working copy",
                        "required_checks": [_check(t, repo_path, ref) for t in rt.sup.qc_tools if t.required],
                        "optional_checks": [t.name for t in rt.sup.qc_tools if not t.required],
                        "model_analysis": use_llm, "hint": CONFIRM_HINT}
            rep = rt.sup.run_qc(str(repo_path), ref, trigger="cli", use_llm=use_llm)
            return {"mode": "done", "verdict": rep.verdict.value, "blockers": rep.blockers,
                    "checks": [_result(c, 3000) for c in rep.checks], "analysis": rep.analysis,
                    "recommendations": rep.recommendations, "report_file": str(rt.sup.last_report_path)}

        return await run_blocking(ctx, work, approver=FixedApprover(confirm), label="running quality checks")

    @mcp.tool(annotations=annotations("Read a repository file", read_only=True))
    async def repo_read_file(repo: str, path: str, ctx: Context, start_line: int = 1,
                             max_lines: int = 80) -> dict[str, Any]:
        """Read lines of a file inside an allowed repository (e.g. to explain a failed check). `path` is relative
        to the repository root; paths that leave the repository are refused."""

        def work(rt: Runtime) -> dict[str, Any]:
            root = _repo(rt, repo)
            f = (root / path).resolve()
            if root not in f.parents or not f.is_file():
                raise ToolError(f"'{path}' is not a file inside {root}")
            if f.stat().st_size > 5_000_000:
                raise ToolError(f"'{path}' is larger than 5 MB")
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, start_line)
            chunk = lines[start - 1: start - 1 + max(1, min(max_lines, 500))]
            text = "\n".join(f"{n}: {line}" for n, line in enumerate(chunk, start))
            return {"repo": str(root), "path": path, "total_lines": len(lines),
                    "text": untrusted_verbatim(text, "repository_file", rt.cfg.max_text_chars),
                    "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work)
