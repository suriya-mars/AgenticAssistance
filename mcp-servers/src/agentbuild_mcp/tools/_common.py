"""Shared tool annotations and small helpers.

Annotations tell MCP clients how careful to be (many show a confirmation for non-read-only tools).
openWorldHint=True marks tools that talk to something outside this machine (the mail server).
"""
from __future__ import annotations

import re

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations


def annotations(title: str, *, read_only: bool = False, destructive: bool = False,
                idempotent: bool = False, open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=read_only, destructiveHint=destructive,
                           idempotentHint=read_only or idempotent, openWorldHint=open_world)


CONFIRM_HINT = ("Nothing was changed. Show this preview to the user; if they agree, call this tool again "
                "with confirm=true.")

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,199}")


def safe_id(value: str, what: str) -> str:
    """Ids that end up in file names (mail uids, run ids) must not contain path separators or '..'."""
    if not _SAFE_ID.fullmatch(value) or ".." in value:
        raise ToolError(f"invalid {what}: {value!r}")
    return value
