"""Policy engine: decides whether an agent may call a tool in the current context."""
from __future__ import annotations

from dataclasses import dataclass, field

from agentbuild.core.types import Decision, RunContext, Risk
from agentbuild.tools.registry import Tool


@dataclass
class Policy:
    # Agents that may ever run EXEC tools.
    exec_agents: set[str] = field(default_factory=lambda: {"qc"})
    # EXEC in a mail-triggered run always needs a human approval.
    approve_exec_on_mail_trigger: bool = True
    # Always ask before any EXEC call, regardless of trigger.
    approve_all_exec: bool = False
    # Senders allowed to trigger QC runs by email (lower-cased addresses or @domain).
    qc_sender_allowlist: list[str] = field(default_factory=list)

    def check(self, agent: str, allowed: list[Tool], tool: Tool, ctx: RunContext) -> tuple[Decision, str]:
        if tool not in allowed:
            return Decision.DENY, f"tool '{tool.name}' not in allowlist for agent '{agent}'"
        if tool.risk is Risk.EXEC:
            if agent not in self.exec_agents:
                return Decision.DENY, f"agent '{agent}' may not run EXEC tools"
            if ctx.tainted:
                # Untrusted text in context + process execution = injection risk. Hard deny.
                return Decision.DENY, "EXEC tools are blocked in a context containing untrusted content"
            if self.approve_all_exec or (ctx.trigger == "mail" and self.approve_exec_on_mail_trigger):
                return Decision.NEEDS_APPROVAL, "EXEC requires human approval"
        return Decision.ALLOW, "ok"

    def sender_may_trigger_qc(self, sender: str) -> bool:
        s = sender.lower().strip()
        if "<" in s:
            s = s[s.index("<") + 1 : s.index(">")] if ">" in s else s
        for entry in self.qc_sender_allowlist:
            e = entry.lower()
            if (e.startswith("@") and s.endswith(e)) or s == e:
                return True
        return False
