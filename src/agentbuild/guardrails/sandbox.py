"""Sandboxed process execution for QC tools: argv only, cwd jail, env scrub, timeout."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAFE_ENV_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "TMPDIR", "VIRTUAL_ENV"}


class SandboxError(Exception):
    pass


@dataclass
class ProcResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def resolve_repo(repo: str, allowed_roots: list[Path]) -> Path:
    """Resolve `repo` and ensure it lies under one of the allowed roots (blocks ../ and symlink escapes)."""
    p = Path(repo).expanduser().resolve()
    if not p.is_dir():
        raise SandboxError(f"repo path does not exist or is not a directory: {p}")
    for root in allowed_roots:
        r = root.expanduser().resolve()
        if p == r or r in p.parents:
            return p
    raise SandboxError(f"repo path {p} is outside allowed roots {[str(r) for r in allowed_roots]}")


def run(argv: list[str], cwd: Path, timeout_s: int = 300, extra_env: dict[str, str] | None = None,
        max_output: int = 200_000) -> ProcResult:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise SandboxError("argv must be a non-empty list of strings")
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS}
    env.update(extra_env or {})
    try:
        cp = subprocess.run(
            argv, cwd=cwd, env=env, shell=False, capture_output=True, text=True,
            timeout=timeout_s, stdin=subprocess.DEVNULL,
        )
        return ProcResult(argv, cp.returncode, cp.stdout[-max_output:], cp.stderr[-max_output:])
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return ProcResult(argv, -1, out[-max_output:], f"timed out after {timeout_s}s", timed_out=True)
    except FileNotFoundError:
        return ProcResult(argv, 127, "", f"executable not found: {argv[0]}")
