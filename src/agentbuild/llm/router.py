"""Maps logical model roles (fast/main) to concrete Ollama model tags from settings."""
from __future__ import annotations


class ModelRouter:
    def __init__(self, models: dict[str, str]):
        self.models = models

    def resolve(self, name: str) -> str:
        # Agents reference roles ("fast", "main"); a literal tag like "qwen2.5:7b" passes through.
        return self.models.get(name, name)
