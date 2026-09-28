"""Thin client for Ollama's native /api/chat endpoint (tool calling + JSON-schema outputs)."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

import httpx

from agentbuild.core.types import LLMResponse, ToolCall

# Some reasoning models inline their thoughts even when `think` is off.
_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLM(Protocol):
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        num_ctx: int = 8192,
        temperature: float = 0.2,
        keep_alive: str = "5m",
        timeout_s: float = 300.0,
        retries: int = 2,
        think: bool | None = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.options = {"num_ctx": num_ctx, "temperature": temperature}
        self.keep_alive = keep_alive
        self.retries = retries
        self.think = think  # Qwen3/DeepSeek-R1 style reasoning; off = much faster structured output
        self._http = httpx.Client(timeout=timeout_s)

    def chat(self, model, messages, tools=None, schema=None) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": self.options,
            "keep_alive": self.keep_alive,
        }
        if self.think is not None:
            body["think"] = self.think
        if tools:
            body["tools"] = tools
        if schema:
            body["format"] = schema

        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self._http.post(f"{self.base_url}/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"Ollama request failed: {last_err}") from last_err

        msg = data.get("message", {})
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args))
        content = _THINK.sub("", msg.get("content", "")).strip()
        return LLMResponse(content=content, tool_calls=calls, raw=data)

    def list_models(self) -> list[str]:
        r = self._http.get(f"{self.base_url}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
