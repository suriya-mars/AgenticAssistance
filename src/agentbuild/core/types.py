"""Core data types shared by the harness, tools and guardrails."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Risk(str, Enum):
    READ = "read"    # no side effects
    WRITE = "write"  # local side effects (drafts, tasks)
    EXEC = "exec"    # runs external processes (QC tools)


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Budget:
    max_steps: int = 8
    max_tool_calls: int = 12
    timeout_s: float = 300.0
    max_tool_result_chars: int = 6000


@dataclass
class RunContext:
    """Per-run state. `tainted` is set once untrusted content (email) enters the context."""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trigger: str = "cli"            # cli | mail
    tainted: bool = False
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class AgentError(Exception):
    pass


class BudgetExceeded(AgentError):
    pass
