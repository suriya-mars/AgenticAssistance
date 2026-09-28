"""Loads QC tools from config and registers them (plus repo-scoped helpers) in the ToolRegistry."""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import yaml
from pydantic import BaseModel, Field

from agentbuild.core.types import RunContext, Risk
from agentbuild.guardrails import sandbox
from agentbuild.tools.qc.base import QCTool
from agentbuild.tools.qc.command_tool import CommandQCTool, CommandToolConfig, safe_ref
from agentbuild.tools.registry import Tool, ToolRegistry


def load_qc_tools(path: Path) -> list[QCTool]:
    data = yaml.safe_load(path.read_text()) or {}
    return [CommandQCTool(CommandToolConfig(**t)) for t in data.get("tools", [])]


class NoArgs(BaseModel):
    pass


class FsReadArgs(BaseModel):
    path: str = Field(description="File path relative to the repository root")
    start_line: int = Field(1, ge=1)
    max_lines: int = Field(80, ge=1, le=300)


def register_qc_tools(registry: ToolRegistry, qc_tools: list[QCTool]) -> None:
    """Each QC tool becomes an EXEC tool 'qc_<name>'. The workspace comes from the run context,
    never from LLM arguments, so the model cannot point a tool at an arbitrary path."""
    for qt in qc_tools:
        def fn(args: NoArgs, ctx: RunContext, _qt=qt):
            res = _qt.run(Path(ctx.meta["workspace"]), ctx.meta.get("ref"))
            ctx.meta.setdefault("qc_results", {})[_qt.name] = res
            return res.model_dump(exclude={"log_tail"}) | {"log_tail": res.log_tail[-1500:]}
        registry.register(Tool(f"qc_{qt.name}", f"Run QC check '{qt.name}': {qt.description}",
                               NoArgs, fn, Risk.EXEC))

    def fs_read(args: FsReadArgs, ctx: RunContext) -> str:
        root = Path(ctx.meta["workspace"]).resolve()
        p = (root / args.path).resolve()
        if root not in p.parents or not p.is_file():
            return f"ERROR: '{args.path}' is not a file inside the repository"
        lines = p.read_text(errors="replace").splitlines()
        chunk = lines[args.start_line - 1 : args.start_line - 1 + args.max_lines]
        return "\n".join(f"{i}: {l}" for i, l in enumerate(chunk, args.start_line))

    # Repo content is third-party text too -> untrusted; reading it blocks further EXEC calls.
    registry.register(Tool("fs_read", "Read lines of a file in the repository under QC",
                           FsReadArgs, fs_read, Risk.READ, untrusted_output=True))


@contextlib.contextmanager
def workspace(repo: Path, ref: str | None) -> Iterator[Path]:
    """Yield a directory to run QC in. For a ref, use a throwaway detached git worktree
    so the user's working copy is never modified."""
    if ref is None:
        yield repo
        return
    r = safe_ref(ref)
    tmp = Path(tempfile.mkdtemp(prefix="agentqc-"))
    wt = tmp / "wt"
    p = sandbox.run(["git", "worktree", "add", "--detach", str(wt), r], cwd=repo, timeout_s=120)
    if p.exit_code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise sandbox.SandboxError(f"git worktree add failed: {p.stderr.strip()}")
    try:
        yield wt
    finally:
        sandbox.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, timeout_s=60)
        shutil.rmtree(tmp, ignore_errors=True)
