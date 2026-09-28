from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from agentbuild.config import load_settings
from agentbuild.core.approval import StaticApprover
from agentbuild.core.supervisor import Supervisor
from agentbuild.core.types import LLMResponse, ToolCall
from agentbuild.mail.imap_source import LocalEmlSource
from agentbuild.storage.db import DB
from agentbuild.tools.qc.loader import load_qc_tools

ROOT = Path(__file__).resolve().parents[1]


class FakeLLM:
    """Scripted LLM. `script` items are LLMResponse, dict (-> JSON content) or callable(messages)->either."""
    def __init__(self, script: list[Any] | Callable[..., Any]):
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def chat(self, model, messages, tools=None, schema=None) -> LLMResponse:
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools, "schema": schema})
        item = self.script(messages) if callable(self.script) else self.script.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, dict):
            return LLMResponse(content=json.dumps(item))
        return item


def tool_call(name: str, **args) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCall(name, args)])


@pytest.fixture
def settings(tmp_path):
    s = load_settings(ROOT)
    s.raw["paths"] = {k: str(tmp_path / "data" / v.split("/")[-1]) for k, v in s.raw["paths"].items()}
    repos = tmp_path / "repos"
    shutil.copytree(ROOT / "samples" / "sample_repo", repos / "sample_repo")
    # The sample repo is stored as plain files in git; give the copy a history for --ref (worktree) tests.
    git = ["git", "-c", "user.email=test@local", "-c", "user.name=test", "-c", "init.defaultBranch=main"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "sample"]):
        subprocess.run(git + args, cwd=repos / "sample_repo", check=True)
    s.raw["qc"]["allowed_repo_roots"] = [str(repos)]
    return s


@pytest.fixture
def make_supervisor(settings):
    def _make(llm, approve: bool = True):
        approver = StaticApprover(approve)
        sup = Supervisor(settings, llm, approver, DB(settings.path("db")),
                         LocalEmlSource(ROOT / "samples" / "mail"), load_qc_tools(settings.qc_tools_file()))
        return sup, approver
    return _make
