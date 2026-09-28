"""The agent harness: a bounded tool-calling loop with guardrails on every hop."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agentbuild.core.approval import Approver
from agentbuild.core.types import AgentError, Budget, BudgetExceeded, Decision, RunContext
from agentbuild.guardrails.output import parse_structured, redact
from agentbuild.guardrails.policy import Policy
from agentbuild.llm.ollama_client import LLM
from agentbuild.llm.router import ModelRouter
from agentbuild.storage.trace import NullTracer, Tracer
from agentbuild.tools.registry import Tool, ToolRegistry


@dataclass
class AgentSpec:
    name: str
    model: str                       # role ("fast"/"main") or literal Ollama tag
    system_prompt: str
    output_model: type[BaseModel]
    tools: list[str] = field(default_factory=list)   # allowlist patterns, e.g. ["mail_get", "qc_*"]
    budget: Budget = field(default_factory=Budget)
    output_retries: int = 2


class Agent:
    def __init__(self, spec: AgentSpec, llm: LLM, router: ModelRouter, registry: ToolRegistry,
                 policy: Policy, approver: Approver, tracer: Tracer | NullTracer | None = None):
        self.spec = spec
        self.llm = llm
        self.model = router.resolve(spec.model)
        self.registry = registry
        self.policy = policy
        self.approver = approver
        self.tracer = tracer or NullTracer()
        self.tools: list[Tool] = registry.resolve(spec.tools)

    # ------------------------------------------------------------------ public
    def run(self, task: str, ctx: RunContext) -> BaseModel:
        b = self.spec.budget
        schema = self.spec.output_model.model_json_schema()
        system = (
            f"{self.spec.system_prompt}\n\n"
            "When you have finished, reply ONLY with a JSON object matching this schema "
            f"(no prose, no code fences):\n{json.dumps(schema)}"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        tool_specs = [t.schema() for t in self.tools] or None
        self.tracer.log("agent_start", agent=self.spec.name, model=self.model,
                        tools=[t.name for t in self.tools], trigger=ctx.trigger, tainted=ctx.tainted)

        for step in range(b.max_steps):
            self._check_time(ctx)
            resp = self.llm.chat(self.model, messages, tools=tool_specs,
                                 schema=None if tool_specs else schema)
            self.tracer.log("llm_response", agent=self.spec.name, step=step, content=resp.content[:2000],
                            tool_calls=[{"name": c.name, "args": c.arguments} for c in resp.tool_calls])

            if not resp.tool_calls:
                return self._finalize(messages, resp.content, schema)

            messages.append({
                "role": "assistant", "content": resp.content,
                "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}} for c in resp.tool_calls],
            })
            for call in resp.tool_calls:
                result = self._handle_call(call.name, call.arguments, ctx)
                messages.append({"role": "tool", "tool_name": call.name, "content": result})

        raise BudgetExceeded(f"agent '{self.spec.name}' exceeded {b.max_steps} steps")

    # ----------------------------------------------------------------- helpers
    def _check_time(self, ctx: RunContext) -> None:
        if time.monotonic() - ctx.started > self.spec.budget.timeout_s:
            raise BudgetExceeded(f"agent '{self.spec.name}' exceeded {self.spec.budget.timeout_s}s")

    def _handle_call(self, name: str, arguments: dict[str, Any], ctx: RunContext) -> str:
        """Run one tool call through policy -> approval -> validation -> execution. Always returns text."""
        b = self.spec.budget
        ctx.tool_calls += 1
        if ctx.tool_calls > b.max_tool_calls:
            raise BudgetExceeded(f"agent '{self.spec.name}' exceeded {b.max_tool_calls} tool calls")

        tool = self.registry.get(name)
        if tool is None:
            self.tracer.log("tool_unknown", tool=name)
            return f"ERROR: unknown tool '{name}'. Available: {[t.name for t in self.tools]}"

        decision, reason = self.policy.check(self.spec.name, self.tools, tool, ctx)
        self.tracer.log("policy", tool=name, decision=decision.value, reason=reason)
        if decision is Decision.DENY:
            return f"DENIED by policy: {reason}"

        try:
            args = self.registry.validate(tool, arguments)
        except ValidationError as e:
            return f"ERROR: invalid arguments for '{name}': {e.errors(include_url=False)}"

        if decision is Decision.NEEDS_APPROVAL:
            detail = f"agent={self.spec.name} tool={name} args={args.model_dump()}"
            ok = self.approver.approve(f"Run {tool.risk.value.upper()} tool '{name}'", detail)
            self.tracer.log("approval", tool=name, approved=ok)
            if not ok:
                return "DENIED: the human reviewer rejected this action. Do not retry it."

        try:
            out = tool.fn(args, ctx)
        except Exception as e:  # tool failures go back to the model, not up the stack
            self.tracer.log("tool_error", tool=name, error=repr(e))
            return f"ERROR: tool '{name}' failed: {e}"

        if tool.untrusted_output:
            ctx.tainted = True
        text = out if isinstance(out, str) else json.dumps(
            out.model_dump() if isinstance(out, BaseModel) else out, default=str)
        text = redact(text)
        if len(text) > b.max_tool_result_chars:
            text = text[: b.max_tool_result_chars] + "\n[...truncated]"
        self.tracer.log("tool_result", tool=name, chars=len(text), tainted=ctx.tainted)
        return text

    def _finalize(self, messages: list[dict[str, Any]], content: str, schema: dict) -> BaseModel:
        """Validate the final answer; on failure, re-ask with JSON-schema constrained decoding."""
        last_err = ""
        for attempt in range(self.spec.output_retries + 1):
            try:
                out = parse_structured(content, self.spec.output_model)
                self.tracer.log("agent_output", agent=self.spec.name, output=out.model_dump())
                return out
            except (ValidationError, ValueError) as e:
                last_err = str(e)[:1500]
                self.tracer.log("output_invalid", agent=self.spec.name, attempt=attempt, error=last_err)
                if attempt == self.spec.output_retries:
                    break
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"Your answer did not match the schema: {last_err}\n"
                                                "Reply again with ONLY the corrected JSON object."},
                ]
                content = self.llm.chat(self.model, messages, tools=None, schema=schema).content
        raise AgentError(f"agent '{self.spec.name}' produced invalid output: {last_err}")
