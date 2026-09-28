"""Loads settings.yaml / agents.yaml / policies.yaml from the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentbuild.guardrails.policy import Policy


def find_root() -> Path:
    env = os.environ.get("AGENTBUILD_HOME")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "config" / "settings.yaml").exists():
            return d
    return Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    root: Path
    raw: dict[str, Any]
    agents: dict[str, Any]
    policy: Policy

    def path(self, key: str) -> Path:
        p = Path(os.path.expanduser(self.raw["paths"][key]))
        return p if p.is_absolute() else self.root / p

    @property
    def ollama(self) -> dict[str, Any]:
        return self.raw.get("ollama", {})

    @property
    def timeout_scale(self) -> float:
        """>1 on slow hardware (CPU-only): stretches agent time budgets and the Ollama request timeout."""
        return max(float(self.ollama.get("timeout_scale", 1)), 0.1)

    @property
    def models(self) -> dict[str, str]:
        return self.raw.get("models", {})

    @property
    def mail(self) -> dict[str, Any]:
        return self.raw.get("mail", {})

    @property
    def docs(self) -> dict[str, Any]:
        return self.raw.get("docs", {})

    @property
    def qc(self) -> dict[str, Any]:
        return self.raw.get("qc", {})

    def qc_tools_file(self) -> Path:
        p = Path(self.qc.get("tools_file", "config/qc_tools.yaml"))
        return p if p.is_absolute() else self.root / p

    def allowed_repo_roots(self) -> list[Path]:
        roots = [Path(os.path.expanduser(p)) for p in self.qc.get("allowed_repo_roots", [])]
        return [r if r.is_absolute() else self.root / r for r in roots]

    def prompt(self, name: str) -> str:
        return (self.root / "prompts" / name).read_text()


def _env_overrides(raw: dict[str, Any]) -> None:
    """A few settings can come from the environment (used by Docker; settings.yaml stays the default)."""
    env = os.environ
    if v := env.get("AGENT_OLLAMA_URL"):
        raw.setdefault("ollama", {})["url"] = v
    if v := env.get("AGENT_OLLAMA_THINK"):
        raw.setdefault("ollama", {})["think"] = v.strip().lower() in ("1", "true", "yes", "on")
    if v := env.get("AGENT_MODEL_FAST"):
        raw.setdefault("models", {})["fast"] = v
    if v := env.get("AGENT_MODEL_MAIN"):
        raw.setdefault("models", {})["main"] = v
    if v := env.get("AGENT_TIMEOUT_SCALE"):
        raw.setdefault("ollama", {})["timeout_scale"] = float(v)
    if v := env.get("AGENT_REPO_ROOTS"):
        raw.setdefault("qc", {})["allowed_repo_roots"] = [p for p in v.split(":") if p]


def load_settings(root: Path | None = None) -> Settings:
    root = root or find_root()
    cfg = root / "config"
    raw = yaml.safe_load((cfg / "settings.yaml").read_text()) or {}
    _env_overrides(raw)
    agents = (yaml.safe_load((cfg / "agents.yaml").read_text()) or {}).get("agents", {})
    pol = yaml.safe_load((cfg / "policies.yaml").read_text()) or {}
    policy = Policy(
        exec_agents=set(pol.get("exec_agents", ["qc"])),
        approve_exec_on_mail_trigger=pol.get("approve_exec_on_mail_trigger", True),
        approve_all_exec=pol.get("approve_all_exec", False),
        qc_sender_allowlist=pol.get("qc_sender_allowlist", []),
    )
    return Settings(root, raw, agents, policy)
