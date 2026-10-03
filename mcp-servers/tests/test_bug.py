"""The BUG process over MCP: previews before changes, and approvals answered through needs_decision."""
from __future__ import annotations

import pytest

from agentbuild_mcp.approvals import DecisionApprover, NeedsDecision, decision_id
from conftest import SECURITY, err, ok

pytestmark = pytest.mark.anyio
BUG = "BUG-12345"


async def _ready_for_security_reply(client) -> None:
    await ok(client, "bug_create", bug_id="12345", title="Payments API order lookup fix")
    assert (await ok(client, "bug_scan", bug_id=BUG, report_path="files/asoc_pass.pdf"))["scan"]["gate"] == "PASS"
    await ok(client, "bug_draft", bug_id=BUG, which="security", confirm=True)


async def test_full_round_trip(client, home, box):
    st = await ok(client, "bug_create", bug_id="12345", title="Payments API order lookup fix")
    assert st["bug_id"] == BUG and st["next"][0]["tool"].startswith("bug_scan")

    # Round 1: the scan fails the gate -> blocked; the error message points to the MCP tools.
    scan = await ok(client, "bug_scan", bug_id=BUG, report_path="files/asoc_fail.pdf")
    assert scan["scan"]["gate"] == "FAIL" and scan["blocked"]["by"] == "asoc_gate"
    assert scan["next"][0]["step"] == "restart"
    msg = await err(client, "bug_draft", bug_id=BUG, which="security")
    assert "BLOCKED" in msg and "bug_restart" in msg

    # Restart: preview changes nothing, confirm starts round 2.
    prev = await ok(client, "bug_restart", bug_id=BUG, fix_note="parameterized the order query")
    assert prev["mode"] == "preview" and prev["new_round"] == 2
    assert (await ok(client, "bug_status", bug_id=BUG))["round"] == 1
    assert (await ok(client, "bug_restart", bug_id=BUG, fix_note="parameterized the order query",
                     confirm=True))["bug"]["round"] == 2

    # Round 2: scan passes; the draft preview saves nothing, confirm saves.
    assert (await ok(client, "bug_scan", bug_id=BUG, report_path="files/asoc_pass.pdf"))["scan"]["gate"] == "PASS"
    prev = await ok(client, "bug_draft", bug_id=BUG, which="security")
    assert prev["mode"] == "preview" and not prev["drafts"][0]["saved"]
    assert "re-submission #1" in prev["drafts"][0]["body"] and prev["drafts"][0]["attachments"] == ["asoc_pass.pdf"]
    drafts_dir = home / "data" / "drafts" / "bugs"
    assert not drafts_dir.exists() or not list(drafts_dir.iterdir())
    saved = await ok(client, "bug_draft", bug_id=BUG, which="security", confirm=True)
    assert saved["drafts"][0]["saved"] and len(list(drafts_dir.glob("*.eml"))) == 1

    # The security team approves: bug_sync asks first, applies only after the answer.
    box.add(f"AppSec <{SECURITY}>", f"Re: [{BUG}] ASoC security scan review", "Reviewed. Approved from security.")
    r = await ok(client, "bug_sync", bug_id=BUG)
    assert r["status"] == "needs_decision" and "Security team approved" in r["decision"]["question"]
    assert "AppSec" in r["decision"]["details"]
    status = await ok(client, "bug_status", bug_id=BUG)
    assert not next(s for s in status["steps"] if s["key"] == "security_approval")["done"]

    r = await ok(client, "bug_sync", bug_id=BUG, decisions={r["decision"]["id"]: "approve"})
    assert r["status"] == "done" and r["replies"][0]["action"] == "applied"
    assert next(s for s in r["bug"]["steps"] if s["key"] == "security_approval")["done"]
    assert (await ok(client, "bug_sync", bug_id=BUG))["replies"][0]["action"] == "seen"      # not asked again

    ans = await ok(client, "bug_ask", bug_id=BUG, question="What is next?")
    assert ans["sources"] == ["status"] and ans["cited_but_not_in_context"] == ["mail:99"]

    listed = (await ok(client, "bug_list"))["bugs"]
    assert listed[0]["bug_id"] == BUG and listed[0]["progress"] == "3/9"

    assert (await ok(client, "bug_delete", bug_id=BUG))["mode"] == "preview"
    await ok(client, "bug_delete", bug_id=BUG, confirm=True)
    assert (await ok(client, "bug_list"))["bugs"] == []


async def test_security_rejection_needs_an_answer_before_blocking(client, box):
    await _ready_for_security_reply(client)
    box.add(SECURITY, f"Re: [{BUG}] ASoC security scan review", "TLS 1.0 is still on. Not acceptable.")
    r = await ok(client, "bug_sync", bug_id=BUG)
    assert r["status"] == "needs_decision" and "Block the process?" in r["decision"]["question"]
    assert not (await ok(client, "bug_status", bug_id=BUG))["blocked"]
    r = await ok(client, "bug_sync", bug_id=BUG, decisions={r["decision"]["id"]: "approve"})
    assert r["replies"][0]["action"] == "blocked_by_security"
    assert r["bug"]["blocked"]["by"] == "security_team"


async def test_rejecting_an_approval_mail_dismisses_it(client, box):
    await _ready_for_security_reply(client)
    box.add(SECURITY, f"Re: [{BUG}] ASoC security scan review", "Approved.")
    r = await ok(client, "bug_sync", bug_id=BUG)
    r = await ok(client, "bug_sync", bug_id=BUG, decisions={r["decision"]["id"]: "reject"})
    assert r["replies"][0]["action"] == "dismissed"
    assert not next(s for s in r["bug"]["steps"] if s["key"] == "security_approval")["done"]


async def test_unlisted_sender_never_counts(client, box):
    await _ready_for_security_reply(client)
    box.add("someone@gmail.com", f"Re: [{BUG}] ASoC security scan review", "Approved, signed off.")
    r = await ok(client, "bug_sync", bug_id=BUG)
    assert r["status"] == "done" and r["replies"][0]["action"] == "ignored_sender"


async def test_manual_mark_previews_and_respects_order(client):
    await ok(client, "bug_create", bug_id="777", title="x")
    prev = await ok(client, "bug_mark_step", bug_id="BUG-777", step="pm_signoff", note="agreed in the meeting")
    assert prev["mode"] == "preview" and prev["would_fail"]
    assert "needs" in await err(client, "bug_mark_step", bug_id="BUG-777", step="pm_signoff", note="x", confirm=True)
    done = await ok(client, "bug_mark_step", bug_id="BUG-777", step="pm_signoff", note="x", confirm=True, force=True)
    assert next(s for s in done["bug"]["steps"] if s["key"] == "pm_signoff")["by"] == "manual: x"
    bad = await err(client, "bug_mark_step", bug_id="BUG-777", step="not_a_step", note="x")
    assert "Input should be 'scan_analyzed'" in bad                  # the client is told the valid steps


def test_decision_ids_ignore_the_models_wording():
    first = "From: A <a@x> (security)\nDate: Mon\nSubject: Re: [BUG-1] x\nReads as: approval - Approves the scan."
    later = "From: A <a@x> (security)\nDate: Mon\nSubject: Re: [BUG-1] x\nReads as: approval - The team approved it."
    q = "Mark 'Security team approved' for BUG-1?"
    assert decision_id(q, first) == decision_id(q, later)
    assert decision_id(q, first) != decision_id("Mark 'Tester approved' for BUG-1?", first)
    approver = DecisionApprover({decision_id(q, first): "approve"})
    assert approver.approve(q, later) is True
    with pytest.raises(NeedsDecision):
        approver.approve("Mark 'Tester approved' for BUG-1?", first)
