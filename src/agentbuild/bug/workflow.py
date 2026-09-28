"""The BUG quality process as an ordered set of steps. Pure logic: no IO, no LLM.

Action steps are done by you through the agent (analyze, draft). Event steps are replies from people,
detected in the inbox and confirmed by you. A step can only complete when its prerequisites are done.
A failed ASoC gate or a security-team rejection BLOCKS the bug; `restart` then begins a new round
(same bug id, steps cleared, history kept).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    kind: str                          # "action" | "event"
    requires: tuple[str, ...] = ()
    role: str | None = None            # event steps: whose reply completes it
    accepts: frozenset[str] = field(default_factory=frozenset)  # mail kinds that complete it
    hint: str = ""


STEPS: tuple[Step, ...] = (
    Step("scan_analyzed", "ASoC scan analyzed", "action", (),
         hint="agent bug scan {bug_id} <asoc-report.pdf>"),
    Step("security_mail", "Security team mail drafted", "action", ("scan_analyzed",),
         hint="agent bug draft {bug_id} security"),
    Step("security_approval", "Security team approved", "event", ("security_mail",), "security",
         frozenset({"approval", "signoff"}),
         hint="waiting for the security team's approval mail → agent bug sync {bug_id}"),
    Step("tester_mail", "Tester mail drafted", "action", ("security_approval",),
         hint="agent bug draft {bug_id} tester"),
    Step("tester_approval", "Tester approved", "event", ("tester_mail",), "tester",
         frozenset({"approval", "signoff"}), hint="waiting for the tester's approval mail → agent bug sync {bug_id}"),
    Step("tester_signoff", "Tester signed off", "event", ("tester_approval",), "tester",
         frozenset({"signoff"}), hint="waiting for the tester's sign-off mail → agent bug sync {bug_id}"),
    Step("signoff_mails", "PM + key leader sign-off mails drafted", "action", ("tester_signoff",),
         hint="agent bug draft {bug_id} signoff"),
    Step("pm_signoff", "Project manager signed off", "event", ("signoff_mails",), "pm",
         frozenset({"approval", "signoff"}), hint="waiting for the PM's sign-off mail → agent bug sync {bug_id}"),
    Step("keyleader_signoff", "Key leader signed off", "event", ("signoff_mails",), "keyleader",
         frozenset({"approval", "signoff"}), hint="waiting for the key leader's sign-off mail → agent bug sync {bug_id}"),
)
BY_KEY = {s.key: s for s in STEPS}

# `agent bug draft <bug> <which>` -> (step completed, [(template, recipient role), ...])
DRAFTS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "security": ("security_mail", [("security", "security")]),
    "tester": ("tester_mail", [("tester", "tester")]),
    "signoff": ("signoff_mails", [("pm_signoff", "pm"), ("keyleader_signoff", "keyleader")]),
}


def missing_prereqs(step: str, done: set[str]) -> list[str]:
    return [r for r in BY_KEY[step].requires if r not in done]


def is_complete(done: set[str]) -> bool:
    return all(s.key in done for s in STEPS)


def next_steps(done: set[str]) -> list[Step]:
    """Steps that are not done and whose prerequisites are all done (PM and key leader run in parallel)."""
    return [s for s in STEPS if s.key not in done and not missing_prereqs(s.key, done)]


def steps_for_reply(role: str | None, kind: str) -> list[str]:
    """Which event steps a reply of `kind` from someone in `role` can complete, in order."""
    if role is None:
        return []
    return [s.key for s in STEPS if s.kind == "event" and s.role == role and kind in s.accepts]


def normalize_bug_id(raw: str, pattern: str, prefix: str) -> str:
    """'12345' -> 'BUG-12345'; ' bug-12345 ' -> 'BUG-12345'. Raises ValueError on anything else."""
    s = raw.strip().upper()
    if s.isdigit() and prefix:
        s = f"{prefix}{s}"
    if not re.fullmatch(pattern, s):
        raise ValueError(f"'{raw}' is not a valid bug id (expected pattern {pattern}, e.g. {prefix}12345)")
    return s


def mentions_bug(bug_id: str, text: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(bug_id)}(?![0-9])", text.upper()) is not None
