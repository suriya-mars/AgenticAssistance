"""Document tools, and the folder allowlist that keeps clients away from files like ~/.ssh."""
from __future__ import annotations

import pytest

from conftest import err, make_pdf, ok

pytestmark = pytest.mark.anyio


async def test_add_search_ask_remove(client, home):
    added = await ok(client, "docs_add", path="files/policy.pdf")
    assert added["pages"] == 5 and added["newly_added"]
    assert not (await ok(client, "docs_add", path="files/policy.pdf"))["newly_added"]      # same bytes = same doc

    assert [d["name"] for d in (await ok(client, "docs_list"))["documents"]] == ["policy.pdf"]

    hits = await ok(client, "docs_search", doc="policy", query="hotfix approvals on-call lead", k=2)
    assert hits["passages"][0]["page"] == 4 and "<untrusted_document>" in hits["passages"][0]["text"]

    ans = await ok(client, "docs_ask", doc="policy", question="How many approvals does a hotfix need?")
    assert ans["grounded"] and ans["citations"][0] == {"page": 4, "quote": "A hotfix needs two approvals",
                                                       "status": "exact", "model_said_page": None}

    prev = await ok(client, "docs_remove", doc="policy")
    assert prev["mode"] == "preview" and len((await ok(client, "docs_list"))["documents"]) == 1
    await ok(client, "docs_remove", doc="policy", confirm=True)
    assert (await ok(client, "docs_list"))["documents"] == []


async def test_path_reference_is_added_on_first_use(client):
    ans = await ok(client, "docs_ask", doc="files/policy.pdf", question="How many approvals?")
    assert ans["doc"] == "policy.pdf" and ans["grounded"]


async def test_files_outside_allowed_folders_are_refused(client, home, tmp_path):
    secret = tmp_path / "private.pdf"
    make_pdf(secret, ["salary list"])
    assert "outside the folders" in await err(client, "docs_add", path=str(secret))
    assert "outside the folders" in await err(client, "docs_add", path="files/../../private.pdf")
    assert "outside the folders" in await err(client, "docs_search", doc=str(secret), query="salary")
    (home / "files" / "link.pdf").symlink_to(secret)                                 # symlink escape
    assert "outside the folders" in await err(client, "docs_add", path="files/link.pdf")


async def test_unknown_document_gives_a_clear_error(client):
    assert "matches 0 documents" in await err(client, "docs_ask", doc="nothing-like-this", question="?")
