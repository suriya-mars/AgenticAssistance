"""Mail templates for the BUG process. Plain text files you edit in config/bug_templates/.

File format:
    Subject: [{bug_id}] ...
    <blank line>
    body...

Only known {placeholders} are replaced; any other braces in your text are left untouched.
Unknown placeholders stay visible as {name} so a missing value is obvious in the draft.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_PH = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass
class Rendered:
    subject: str
    body: str
    missing: list[str]


# Placeholders that are legitimately empty (e.g. no re-submission note in round 1).
OPTIONAL = {"resubmission_note", "tester_conditions", "fix_note"}


def render_text(text: str, values: dict[str, object]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in values and values[key] not in (None, ""):
            return str(values[key])
        if key in OPTIONAL and key in values:
            return ""
        missing.append(key)
        return m.group(0)
    return _PH.sub(sub, text), missing


def render(path: Path, values: dict[str, object]) -> Rendered:
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")   # templates edited on Windows
    head, _, body = raw.partition("\n\n")
    if not head.lower().startswith("subject:"):
        raise ValueError(f"{path.name}: first line must be 'Subject: ...' followed by a blank line")
    subject, m1 = render_text(head.split(":", 1)[1].strip(), values)
    text, m2 = render_text(body.strip() + "\n", values)
    text = re.sub(r"\n{3,}", "\n\n", text)   # an empty optional line leaves no gap
    return Rendered(subject, text, sorted(set(m1 + m2)))
