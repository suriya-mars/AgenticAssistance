"""Human-in-the-loop approval. CLI prompt by default; swap for a web/queue approver later."""
from __future__ import annotations

from typing import Protocol

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm


class Approver(Protocol):
    def approve(self, action: str, detail: str) -> bool: ...


class CLIApprover:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def approve(self, action: str, detail: str) -> bool:
        self.console.print(f"\n[bold yellow]Approval required:[/] {escape(action)}\n{escape(detail)}")
        return Confirm.ask("Proceed?", default=False)


class StaticApprover:
    """For tests / non-interactive runs."""
    def __init__(self, answer: bool):
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    def approve(self, action: str, detail: str) -> bool:
        self.asked.append((action, detail))
        return self.answer
