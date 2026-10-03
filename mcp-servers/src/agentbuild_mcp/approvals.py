"""Human confirmation over MCP.

AgentBuild's services ask an Approver before every step a person must confirm: saving a draft, accepting an
approval mail, blocking a bug, running EXEC tools. On the command line that is a y/N prompt. Over MCP the person
sits behind the client, so:

- Tools with `confirm`: confirm=false runs with FixedApprover(False), so nothing is saved and the tool returns a
  preview; confirm=true runs with FixedApprover(True).
- bug_sync: DecisionApprover stops at the first question it has no answer for, and the tool returns it as
  `needs_decision`. The client asks the person, then calls again with decisions={id: "approve" | "reject"}.
  Stopping there is safe: the services record a step only after its approval, and a question that was
  interrupted is simply asked again on the next run.
"""
from __future__ import annotations

import hashlib
from typing import Any


class NeedsDecision(Exception):
    """Raised inside a service call when a person has to answer a question first."""

    def __init__(self, decision_id: str, question: str, details: str):
        super().__init__(question)
        self.decision_id, self.question, self.details = decision_id, question, details

    def as_dict(self) -> dict[str, str]:
        return {"id": self.decision_id, "question": self.question, "details": self.details}


def decision_id(action: str, detail: str) -> str:
    """Stable id for one question about one mail.

    Built from the question and the mail's From/Date/Subject lines only: the rest of `detail` contains the
    model's summary, whose wording differs between the first run and later runs (which use the stored summary).
    """
    keep = [line for line in detail.splitlines() if line.startswith(("From:", "Date:", "Subject:"))]
    return "d-" + hashlib.sha256("\n".join([action, *keep]).encode()).hexdigest()[:10]


class DecisionApprover:
    """Answers from `decisions`; any question without an answer stops the call with NeedsDecision."""

    def __init__(self, decisions: dict[str, str] | None = None):
        self.decisions = {k: v for k, v in (decisions or {}).items()}
        self.answered: list[dict[str, Any]] = []

    def approve(self, action: str, detail: str) -> bool:
        did = decision_id(action, detail)
        answer = self.decisions.get(did)
        if answer not in ("approve", "reject"):
            raise NeedsDecision(did, action, detail)
        self.answered.append({"id": did, "question": action, "answer": answer})
        return answer == "approve"


class FixedApprover:
    """The same answer to every question: False for previews (nothing is saved), True after the user confirmed."""

    def __init__(self, answer: bool):
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    def approve(self, action: str, detail: str) -> bool:
        self.asked.append((action, detail))
        return self.answer
