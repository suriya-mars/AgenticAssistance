"""Tool registry: typed tools (pydantic args) exposed to the LLM as JSON-schema function specs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from agentbuild.core.types import Risk


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., Any]          # fn(args: BaseModel, ctx: RunContext) -> Any
    risk: Risk
    untrusted_output: bool = False  # output carries external content -> taints the run context

    def schema(self) -> dict[str, Any]:
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, args: type[BaseModel], risk: Risk = Risk.READ,
             untrusted_output: bool = False):
        """Decorator form: @registry.tool("mail_get", "...", MailGetArgs, Risk.READ)."""
        def deco(fn):
            self.register(Tool(name, description, args, fn, risk, untrusted_output))
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def resolve(self, patterns: list[str]) -> list[Tool]:
        """Expand an allowlist; a trailing '*' matches by prefix (e.g. 'qc_*')."""
        out = []
        for name, t in self._tools.items():
            if any(name == p or (p.endswith("*") and name.startswith(p[:-1])) for p in patterns):
                out.append(t)
        return out

    def validate(self, tool: Tool, arguments: dict[str, Any]) -> BaseModel:
        return tool.args_model.model_validate(arguments)


__all__ = ["Tool", "ToolRegistry", "ValidationError"]
