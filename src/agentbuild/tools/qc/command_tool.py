"""Generic QC adapter: wraps any CLI tool declared in qc_tools.yaml as a QCTool."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from pydantic import BaseModel, Field

from agentbuild.guardrails import sandbox
from agentbuild.tools.qc.base import QCResult, Severity, Status

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class CommandToolConfig(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    description: str = ""
    argv: list[str]                         # placeholders: {repo}, {ref}
    timeout_s: int = 300
    severity: Severity = Severity.MAJOR
    required: bool = True
    pass_exit_codes: list[int] = [0]
    parser: str = "exit_code"               # exit_code | json | regex
    findings_regex: str | None = None       # parser=regex: one finding per match (named groups kept)
    env: dict[str, str] = {}


def safe_ref(ref: str | None) -> str:
    if ref is None:
        return "HEAD"
    if not _SAFE_REF.match(ref) or ".." in ref:
        raise ValueError(f"unsafe git ref: {ref!r}")
    return ref


class CommandQCTool:
    def __init__(self, cfg: CommandToolConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.description = cfg.description
        self.severity = cfg.severity
        self.required = cfg.required

    def available(self) -> bool:
        return shutil.which(self.cfg.argv[0]) is not None

    def build_argv(self, workspace: Path, ref: str | None) -> list[str]:
        # Placeholders are substituted per-argument; values can never introduce new argv entries.
        subs = {"{repo}": str(workspace), "{ref}": safe_ref(ref)}
        out = []
        for a in self.cfg.argv:
            for k, v in subs.items():
                a = a.replace(k, v)
            out.append(a)
        return out

    def _findings(self, stdout: str) -> list[dict]:
        if self.cfg.parser == "json":
            try:
                data = json.loads(stdout or "null")
            except json.JSONDecodeError:
                return []
            items = data if isinstance(data, list) else data.get("findings", data.get("results", [])) \
                if isinstance(data, dict) else []
            return [i if isinstance(i, dict) else {"value": i} for i in items][:200]
        if self.cfg.parser == "regex" and self.cfg.findings_regex:
            rx = re.compile(self.cfg.findings_regex, re.M)
            return [m.groupdict() or {"match": m.group(0)} for m in rx.finditer(stdout)][:200]
        return []

    def run(self, workspace: Path, ref: str | None) -> QCResult:
        if not self.available():
            return QCResult(name=self.name, status=Status.SKIPPED, severity=self.severity,
                            summary=f"'{self.cfg.argv[0]}' not installed")
        t0 = time.monotonic()
        try:
            argv = self.build_argv(workspace, ref)
        except ValueError as e:
            return QCResult(name=self.name, status=Status.ERROR, severity=self.severity, summary=str(e))
        p = sandbox.run(argv, cwd=workspace, timeout_s=self.cfg.timeout_s, extra_env=self.cfg.env)
        findings = self._findings(p.stdout)
        if p.timed_out or p.exit_code == 127:
            status = Status.ERROR
        elif p.exit_code in self.cfg.pass_exit_codes:
            status = Status.PASS
        else:
            status = Status.FAIL
        tail = (p.stdout + ("\n" + p.stderr if p.stderr else ""))[-3000:]
        return QCResult(
            name=self.name, status=status, severity=self.severity,
            summary=f"exit={p.exit_code}, findings={len(findings)}" + (" (timeout)" if p.timed_out else ""),
            findings=findings, exit_code=p.exit_code, duration_s=round(time.monotonic() - t0, 2),
            log_tail=tail,
        )
