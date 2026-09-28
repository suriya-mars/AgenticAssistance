"""QC plug-in contract. Your organisation's tools implement QCTool (or are declared in qc_tools.yaml)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Severity(str, Enum):
    BLOCKER = "blocker"   # failure => NO_GO
    MAJOR = "major"       # failure => NEEDS_REVIEW
    MINOR = "minor"       # reported only
    INFO = "info"


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"       # tool crashed / timed out
    SKIPPED = "skipped"   # tool not installed / not applicable


class QCResult(BaseModel):
    name: str
    status: Status
    severity: Severity
    summary: str = ""
    findings: list[dict] = Field(default_factory=list)
    exit_code: int | None = None
    duration_s: float = 0.0
    log_tail: str = ""


@runtime_checkable
class QCTool(Protocol):
    name: str
    description: str
    severity: Severity
    required: bool   # run automatically in every QC pipeline

    def run(self, workspace: Path, ref: str | None) -> QCResult: ...
