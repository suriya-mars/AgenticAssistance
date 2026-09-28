"""ASoC (HCL AppScan on Cloud) report helpers: deterministic severity counts, security gate, finding checks.

The LLM reads the report and extracts structure; these functions decide what is trusted:
  - severity counts found by regex in the summary pages win over the LLM's numbers (mismatch -> warning)
  - an extracted issue type is kept only if that text really appears in the report
  - the gate (PASS/FAIL) is computed from counts by rule, never by the model
"""
from __future__ import annotations

import re

from agentbuild.docs.store import normalize

SEVERITIES = ("critical", "high", "medium", "low", "informational")

# Matches "High severity issues: 5", "High 5", "High: 5", "Informational issues - 12", "Info 3"
_COUNT = re.compile(
    r"\b(critical|high|medium|low|informational|info)\b(?:[ \t]+severity)?(?:[ \t]+issues?)?[ \t]*[:\-–]?[ \t]*(\d{1,5})\b(?![.,]\d)",
    re.I,
)
_TOTAL = re.compile(r"\btotal\b[^\n\d]{0,60}?(\d{1,6})\b", re.I)


_META = {
    "application": re.compile(r"^\s*(?:application(?: name)?|app name)\s*[:\-]\s*(.+?)\s*$", re.I | re.M),
    "scan_name": re.compile(r"^\s*scan(?: name)?\s*[:\-]\s*(.+?)\s*$", re.I | re.M),
    "scan_date": re.compile(r"^\s*(?:scan date|date of scan|scan (?:started|executed)(?: on)?)\s*[:\-]\s*(.+?)\s*$",
                            re.I | re.M),
}
# A row of the "Issue Types" table: <name>   <Severity>   <count>
_ISSUE_ROW = re.compile(r"^\s*(?P<issue_type>[A-Za-z][^\n]*?[A-Za-z)])\s{2,}"
                        r"(?P<severity>Critical|High|Medium|Low|Informational)\s+(?P<count>\d{1,5})\s*$", re.M)


def regex_metadata(front_text: str) -> dict[str, str]:
    return {k: m.group(1) for k, rx in _META.items() if (m := rx.search(front_text))}


def regex_issue_table(text: str) -> list[dict]:
    """Rows of ASoC's issue-type table, most severe first."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    rows = [{"issue_type": m["issue_type"].strip(), "severity": m["severity"].lower(), "count": int(m["count"]),
             "location": ""} for m in _ISSUE_ROW.finditer(text)]
    return sorted(rows, key=lambda r: order[r["severity"]])


def merge_issues(llm: list[dict], table: list[dict]) -> list[dict]:
    """Table rows are authoritative (name, severity, count); the model may add a location."""
    if not table:
        return llm
    loc = {normalize(i.get("issue_type", "")): i.get("location", "") for i in llm}
    return [r | {"location": loc.get(normalize(r["issue_type"]), "")} for r in table]


def regex_counts(summary_text: str) -> dict[str, int]:
    """First occurrence per severity in the report's summary pages."""
    out: dict[str, int] = {}
    for m in _COUNT.finditer(summary_text):
        sev = m.group(1).lower()
        sev = "informational" if sev == "info" else sev
        out.setdefault(sev, int(m.group(2)))
    return out


def regex_total(summary_text: str) -> int | None:
    m = _TOTAL.search(summary_text)
    return int(m.group(1)) if m else None


def merge_counts(llm: dict[str, int], rx: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    counts, warnings = {}, []
    if rx and not any(llm.values()):  # model returned nothing useful: one note instead of five
        return {s: rx.get(s, 0) for s in SEVERITIES}, ["model did not read the counts; used the report's summary text"]
    for sev in SEVERITIES:
        if sev in rx:
            counts[sev] = rx[sev]
            if llm.get(sev, rx[sev]) != rx[sev]:
                warnings.append(f"{sev}: model read {llm.get(sev)}, report text says {rx[sev]} (using {rx[sev]})")
        else:
            counts[sev] = llm.get(sev, 0)
            if llm.get(sev):
                warnings.append(f"{sev}: {llm[sev]} comes from the model only; check it in the report")
    return counts, warnings


def verify_issues(issues: list[dict], full_text: str) -> tuple[list[dict], list[str]]:
    """Keep issues whose type text appears in the report (whitespace/case-insensitive)."""
    doc = normalize(full_text)
    kept, dropped = [], []
    for i in issues:
        name = normalize(i.get("issue_type", "")).strip(" .")
        (kept if name and name in doc else dropped).append(i)
    return kept, [d.get("issue_type", "") for d in dropped]


def security_gate(counts: dict[str, int], limits: dict[str, int | None]) -> tuple[str, str]:
    """FAIL if any severity exceeds its limit. A limit of None (or absent) means that severity is not checked."""
    reasons = [f"{counts.get(sev, 0)} {sev} (max {limits[sev]})" for sev in SEVERITIES
               if limits.get(sev) is not None and counts.get(sev, 0) > limits[sev]]
    if reasons:
        return "FAIL", "; ".join(reasons)
    checked = [s for s in SEVERITIES if limits.get(s) is not None]
    return "PASS", f"within limits for {', '.join(checked) or 'no severities'}"


def severity_table(counts: dict[str, int]) -> str:
    w = max(len(s) for s in SEVERITIES) + 2
    lines = [f"  {s.capitalize():<{w}}{counts.get(s, 0):>5}" for s in SEVERITIES]
    lines.append(f"  {'Total':<{w}}{sum(counts.get(s, 0) for s in SEVERITIES):>5}")
    return "\n".join(lines)


def severity_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{counts.get(s, 0)} {s}" for s in SEVERITIES)
