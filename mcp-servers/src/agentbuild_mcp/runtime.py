"""Builds AgentBuild for one tool call and runs the call off the event loop.

A fresh Runtime is built inside the worker thread of every call:
  - SQLite connections only work in the thread that opened them;
  - edits to ../config/*.yaml and templates take effect without restarting the server.
It mirrors how the CLI wires things up (cli._build), minus the terminal prompts and console output:
over stdio, stdout belongs to the MCP protocol.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from functools import cached_property
from pathlib import Path
from typing import Callable, TypeVar

import anyio
import anyio.to_thread
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from agentbuild.bug.service import BugService, WorkflowError
from agentbuild.bug.store import BugStore
from agentbuild.config import Settings, load_settings
from agentbuild.core.approval import Approver
from agentbuild.core.supervisor import Supervisor
from agentbuild.core.types import AgentError
from agentbuild.docs.extract import PdfError
from agentbuild.guardrails.sandbox import SandboxError
from agentbuild.llm.ollama_client import OllamaClient, OllamaUnavailable
from agentbuild.mail.drafts import DraftSink, IMAPDraftSink, LocalDraftSink
from agentbuild.mail.imap_source import IMAPSource, LocalEmlSource, MailSource
from agentbuild.storage.db import DB
from agentbuild.tools.qc.loader import load_qc_tools

from agentbuild_mcp.approvals import DecisionApprover, NeedsDecision
from agentbuild_mcp.guards import McpConfig, in_home, load_config

log = logging.getLogger("agentbuild_mcp")
T = TypeVar("T")

# Failures the user can act on: their message goes back to the client. Anything else is a bug and the SDK
# reports only "Error executing tool" (details go to the server's stderr log).
EXPECTED_ERRORS = (WorkflowError, PdfError, SandboxError, OllamaUnavailable, AgentError,
                   KeyError, ValueError, FileNotFoundError, AssertionError)


# ----------------------------------------------------------------------------------------------- process setup
def load_dotenv(home: Path) -> None:
    """Read AgentBuild's .env (IMAP password, model choices), like the Windows and Docker launchers do.
    Later lines win; variables already set in the real environment win over the file. TZ is skipped."""
    env_file = home / ".env"
    if not env_file.is_file():
        return
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = (s.strip() for s in line.split("=", 1))
            if key and value and key != "TZ":
                values[key] = value.strip("\"'")
    for key, value in values.items():
        os.environ.setdefault(key, value)


def default_timeout_scale() -> None:
    """Without an NVIDIA GPU the models run on the CPU: give them 4x longer time limits (as ./agent does)."""
    os.environ.setdefault("AGENT_TIMEOUT_SCALE", "1" if shutil.which("nvidia-smi") else "4")


# ------------------------------------------------------------------------------------------------ factories
def make_llm(s: Settings) -> OllamaClient:
    """The local model client. Tests replace this function with a scripted fake."""
    o = s.ollama
    return OllamaClient(o.get("url", "http://localhost:11434"), o.get("num_ctx", 8192), o.get("temperature", 0.2),
                        o.get("keep_alive", "5m"), timeout_s=o.get("timeout_s", 300) * s.timeout_scale,
                        think=o.get("think", False))


def make_mail_source(s: Settings) -> MailSource:
    m = s.mail
    if m.get("source") == "imap":
        i = m["imap"]
        return IMAPSource(i["host"], i["user"], i.get("password_env", "AGENT_IMAP_PASSWORD"),
                          i.get("folder", "INBOX"), i.get("port", 993))
    d = Path(m.get("local_dir", "samples/mail"))
    return LocalEmlSource(d if d.is_absolute() else s.root / d)


def make_sink(s: Settings, mail: MailSource) -> DraftSink:
    drafts = s.path("drafts_dir") / "bugs"
    if s.mail.get("draft_target") == "imap" and isinstance(mail, IMAPSource):
        return IMAPDraftSink(mail, s.mail.get("drafts_folder", "Drafts"), drafts)
    return LocalDraftSink(drafts)


class Runtime:
    """Everything one tool call needs. Only ever used from the thread that created it."""

    def __init__(self, cfg: McpConfig, approver: Approver):
        self.cfg = cfg
        self.approver = approver
        self.s = load_settings(cfg.home)
        self.db = DB(self.s.path("db"))
        self.llm = make_llm(self.s)
        self.mail = make_mail_source(self.s)
        self.sup = Supervisor(self.s, self.llm, approver, self.db, self.mail, load_qc_tools(self.s.qc_tools_file()))

    @cached_property
    def bugs(self) -> BugService:
        return BugService(self.sup, BugStore(self.db.conn), make_sink(self.s, self.mail), self.approver)

    def path(self, p: str) -> Path:
        return in_home(self.cfg, p)

    def close(self) -> None:
        self.db.conn.close()


# ------------------------------------------------------------------------------------------------- running
def _message(e: BaseException) -> str:
    msg = str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
    if "agent bug " in msg or "agent pdf " in msg:
        msg += ("\n(Over MCP the commands above are tools: `agent bug restart` = bug_restart, `agent bug scan` = "
                "bug_scan, `agent bug draft` = bug_draft, `agent bug sync` = bug_sync, `agent pdf add` = docs_add.)")
    return msg


async def run_blocking(ctx: Context | None, fn: Callable[[Runtime], T], *, approver: Approver | None = None,
                       label: str = "working", heartbeat_s: float = 10.0) -> T:
    """Run `fn(runtime)` in a worker thread. While it runs, send a progress notification every `heartbeat_s`
    seconds (models on a CPU can take minutes; clients that honour progress keep waiting instead of timing out)."""
    cfg = load_config()
    box: dict[str, object] = {}
    done = anyio.Event()

    def work() -> T:
        rt = Runtime(cfg, approver or DecisionApprover())
        try:
            return fn(rt)
        finally:
            rt.close()

    async def runner() -> None:
        try:
            box["value"] = await anyio.to_thread.run_sync(work)
        except BaseException as e:      # handed to the caller below, after the heartbeat loop ends
            box["error"] = e
        finally:
            done.set()

    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner)
        while not done.is_set():
            with anyio.move_on_after(heartbeat_s):
                await done.wait()
            if not done.is_set() and ctx is not None:
                elapsed = int(time.monotonic() - started)
                try:
                    await ctx.report_progress(elapsed, None, f"{label}: {elapsed}s so far")
                except Exception:       # progress is best effort
                    pass

    if "error" in box:
        e = box["error"]
        assert isinstance(e, BaseException)
        if isinstance(e, (NeedsDecision, ToolError)):
            raise e
        if isinstance(e, EXPECTED_ERRORS):
            raise ToolError(_message(e)) from e
        raise e
    return box["value"]  # type: ignore[return-value]
