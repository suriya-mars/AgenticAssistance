"""Deterministic verdict + report. The LLM may explain results but can never change the verdict."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from agentbuild.tools.qc.base import QCResult, Severity, Status


class Verdict(str, Enum):
    GO = "GO"
    NO_GO = "NO_GO"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class QCReport(BaseModel):
    repo: str
    ref: str | None
    verdict: Verdict
    blockers: list[str]
    checks: list[QCResult]
    analysis: str = ""          # LLM-written explanation (advisory only)
    recommendations: list[str] = []


def compute_verdict(results: list[QCResult]) -> tuple[Verdict, list[str]]:
    blockers, review = [], []
    for r in results:
        bad = r.status in (Status.FAIL, Status.ERROR)
        if r.severity is Severity.BLOCKER and (bad or r.status is Status.SKIPPED):
            blockers.append(f"{r.name}: {r.status.value} - {r.summary}")
        elif r.severity is Severity.MAJOR and bad:
            review.append(r.name)
    if blockers:
        return Verdict.NO_GO, blockers
    if review or not results:
        return Verdict.NEEDS_REVIEW, []
    return Verdict.GO, []


def to_markdown(rep: QCReport) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "skipped": "SKIP"}
    lines = [f"# QC Report - {rep.repo} @ {rep.ref or 'HEAD'}", "", f"**Verdict: {rep.verdict.value}**", ""]
    if rep.blockers:
        lines += ["## Blockers", *[f"- {b}" for b in rep.blockers], ""]
    lines += ["## Checks", "| Check | Severity | Status | Summary | Time |", "|---|---|---|---|---|"]
    for c in rep.checks:
        lines.append(f"| {c.name} | {c.severity.value} | {icon[c.status.value]} | {c.summary} | {c.duration_s}s |")
    if rep.analysis:
        lines += ["", "## Analysis (LLM, advisory)", rep.analysis]
    if rep.recommendations:
        lines += ["", "## Recommendations", *[f"- {r}" for r in rep.recommendations]]
    return "\n".join(lines) + "\n"
