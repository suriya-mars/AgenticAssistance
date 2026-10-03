"""The server as a whole: tool groups, annotations, real stdio and HTTP transports, the HTTP token guard."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from agentbuild_mcp.server import BearerToken, create_server, http_app

pytestmark = pytest.mark.anyio

DOCS = {"system_health", "docs_add", "docs_list", "docs_search", "docs_ask", "docs_remove"}


async def test_groups_annotations_and_descriptions(home):
    tools = {t.name: t for t in await create_server().list_tools()}
    assert len(tools) == 31
    assert {t.name for t in await create_server(["docs"]).list_tools()} == DOCS
    for name in ("docs_list", "bug_status", "mail_read", "repo_read_file", "run_trace", "qc_list_checks"):
        assert tools[name].annotations.read_only_hint, name
    for name in ("bug_delete", "bug_restart", "bug_reopen_step", "docs_remove"):
        assert tools[name].annotations.destructive_hint, name
    for name in ("mail_read", "mail_triage", "bug_sync"):
        assert tools[name].annotations.open_world_hint, name          # they talk to the mail server
    assert all(t.description and len(t.description) > 40 for t in tools.values())
    with pytest.raises(ValueError):
        create_server(["nope"])


async def test_stdio_subprocess_end_to_end(home):
    """A real server process: the protocol only works if nothing else ever reaches stdout."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentbuild_mcp", "--tools", "docs"],
                                   env=dict(os.environ))
    async with Client(params) as c:
        assert {t.name for t in (await c.list_tools()).tools} == DOCS
        added = (await c.call_tool("docs_add", {"path": "files/policy.pdf"})).structured_content
        assert added["pages"] == 5 and added["newly_added"]
        hits = (await c.call_tool("docs_search", {"doc": "policy", "query": "hotfix approvals on-call lead",
                                                  "k": 2})).structured_content
        assert hits["passages"][0]["page"] == 4
        assert "<untrusted_document>" in hits["passages"][0]["text"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_http_transport_on_localhost(home, fake_llm):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(http_app(create_server(["docs"]), "127.0.0.1"), host="127.0.0.1",
                                           port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            assert {t.name for t in (await c.list_tools()).tools} == DOCS
            r = await c.call_tool("docs_add", {"path": "files/policy.pdf"})
            assert r.structured_content["pages"] == 5
    finally:
        server.should_exit = True
        thread.join(5)


async def test_bearer_token_guard():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guard = BearerToken(app, "s3cret-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guard), base_url="http://test") as h:
        assert (await h.post("/mcp")).status_code == 401
        assert (await h.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await h.post("/mcp", headers={"Authorization": "Bearer s3cret-token"})).status_code == 200


def test_http_refuses_network_address_without_token(home, monkeypatch):
    monkeypatch.delenv("AGENT_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="without a token"):
        http_app(create_server(["docs"]), "0.0.0.0")
    monkeypatch.setenv("AGENT_MCP_TOKEN", "long-random-token")
    assert isinstance(http_app(create_server(["docs"]), "0.0.0.0"), BearerToken)
