"""Fixtures: a throw-away AgentBuild home (copied config, generated PDFs, a mailbox, a git repo) and a scripted model.

Tests talk to the server through a real MCP client (in memory, or a stdio subprocess), never to the tool functions
directly, so they cover argument validation, results and errors exactly as a client sees them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.client.client import Client

from agentbuild.core.types import LLMResponse

ROOT = Path(__file__).resolve().parents[2]            # the AgentBuild project (read-only here)
sys.path.insert(0, str(ROOT / "samples"))
from make_asoc_pdf import FIXED_PAGES, PAGES as ASOC_FAIL_PAGES  # noqa: E402
from make_sample_pdf import PAGES as POLICY_PAGES, make_pdf     # noqa: E402

SECURITY = "security-team@yourcompany.com"


def fake_model(messages: list[dict]) -> dict[str, Any]:
    """Plays every AgentBuild agent, recognised by its system prompt."""
    system, task = messages[0]["content"], messages[-1]["content"]
    if "AppScan on Cloud (ASoC)" in system:
        fixed = "release-2.4.1" in task
        return {"application": "Payments API", "scan_name": "", "scan_date": "", "critical": 0,
                "high": 0 if fixed else 2, "medium": 3 if fixed else 5, "low": 4, "informational": 3,
                "top_issues": [], "summary": "Findings summarised."}
    if "classify a reply" in system:
        body = task.split("<untrusted_email>")[-1].lower()
        sig = {"kind": "info", "summary": "FYI.", "conditions": "", "requested_changes": "", "reply_in_thread": False}
        if "not acceptable" in body:
            sig |= {"kind": "rejection", "summary": "Rejected.", "requested_changes": "disable TLS 1.0"}
        elif "signed off" in body or "sign-off" in body:
            sig |= {"kind": "signoff", "summary": "Signs off."}
        elif "approved" in body:
            sig |= {"kind": "approval", "summary": "Approves."}
        return sig
    if "ONE bug's quality process" in system:
        return {"answer": "Waiting for the tester.", "sources": ["status", "mail:99"]}
    if "questions about a PDF document" in system:
        return {"answer": "Two approvals.", "found": True,
                "citations": [{"page": 4, "quote": "A hotfix needs two approvals"}]}
    if "email triage assistant" in system:
        return {"priority": "high", "category": "release", "summary": "Release request for sample_repo.",
                "action_items": [{"title": "QC sample_repo", "due": "", "owner": ""}], "needs_reply": True,
                "is_release_request": True, "repo": "sample_repo", "ref": "main", "suspicious": False}
    if "draft email replies" in system:
        return {"subject": "Re: Please QC sample_repo", "body": "Will do.", "confidence": 0.9}
    if "release quality-check analyst" in system:
        return {"analysis": "All required checks passed.", "recommendations": []}
    raise AssertionError(f"unexpected agent prompt: {system[:80]}")


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, model, messages, tools=None, schema=None) -> LLMResponse:
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        return LLMResponse(content=json.dumps(fake_model(messages)))

    def list_models(self) -> list[str]:
        return ["qwen3:4b", "qwen3.5:9b"]


class Box:
    """The test mailbox (mail.source: local). Mails are dated `offset` s from now."""

    def __init__(self, path: Path):
        self.path, self.n = path, 0

    def add(self, sender: str, subject: str, body: str, offset: int = 60) -> str:
        self.n += 1
        mid = f"<m{self.n}@test>"
        (self.path / f"m{self.n:03d}.eml").write_text(
            f"From: {sender}\nTo: you@example.com\nSubject: {subject}\nMessage-ID: {mid}\n"
            f"Date: {formatdate(time.time() + offset, localtime=True)}\n\n{body}\n")
        return mid


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "agentbuild"
    shutil.copytree(ROOT / "config", h / "config")
    shutil.copytree(ROOT / "prompts", h / "prompts")
    for d in ("files", "samples", "inbox", "repos"):
        (h / d).mkdir()
    make_pdf(h / "files" / "policy.pdf", POLICY_PAGES)
    make_pdf(h / "files" / "asoc_fail.pdf", ASOC_FAIL_PAGES)
    make_pdf(h / "files" / "asoc_pass.pdf", FIXED_PAGES)
    repo = h / "repos" / "sample_repo"
    shutil.copytree(ROOT / "samples" / "sample_repo", repo, ignore=shutil.ignore_patterns("__pycache__"))
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "sample"]):
        subprocess.run(git + args, cwd=repo, check=True)

    settings = yaml.safe_load((h / "config" / "settings.yaml").read_text())
    settings["mail"].update(source="local", local_dir="inbox", draft_target="local")
    settings["qc"]["allowed_repo_roots"] = ["repos"]
    (h / "config" / "settings.yaml").write_text(yaml.safe_dump(settings))

    monkeypatch.setenv("AGENTBUILD_HOME", str(h))
    monkeypatch.setenv("AGENT_MCP_CONFIG", str(tmp_path / "no-mcp.yaml"))      # defaults: files/, samples/
    monkeypatch.setenv("AGENT_TIMEOUT_SCALE", "1")
    for var in ("AGENT_MODEL_FAST", "AGENT_MODEL_MAIN", "AGENT_MODEL_QUALITY", "AGENT_MCP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def box(home: Path) -> Box:
    return Box(home / "inbox")


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    llm = FakeLLM()
    monkeypatch.setattr("agentbuild_mcp.runtime.make_llm", lambda s: llm)
    return llm


@pytest.fixture
async def client(home: Path, fake_llm: FakeLLM):
    from agentbuild_mcp.server import create_server
    async with Client(create_server()) as c:
        yield c


async def ok(c: Client, tool: str, **args: Any) -> dict[str, Any]:
    r = await c.call_tool(tool, args)
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def err(c: Client, tool: str, **args: Any) -> str:
    r = await c.call_tool(tool, args)
    assert r.is_error, f"{tool} should have failed: {r.structured_content}"
    return r.content[0].text
