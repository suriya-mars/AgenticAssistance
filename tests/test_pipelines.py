from pathlib import Path

from agentbuild.core.types import LLMResponse
from agentbuild.tools.qc.report import Verdict

from conftest import FakeLLM, tool_call


def triage_llm(messages):
    """Behaves like a well-aligned model: release mail -> release request; others -> not."""
    email = messages[-1]["content"]
    if "sample_repo (branch main)" in email:
        return {"priority": "high", "category": "release", "summary": "QC request for sample_repo.",
                "action_items": [{"title": "QC sample_repo", "due": "Thursday EOD"},
                                 {"title": "Update release notes"}],
                "needs_reply": True, "is_release_request": True, "repo": "sample_repo", "ref": "main"}
    if "evil-example" in email:
        # Simulates a model that got fooled by the injection - guardrails must still hold.
        return {"priority": "urgent", "category": "request", "summary": "Mailbox full.",
                "is_release_request": True, "repo": "sample_repo", "ref": "main"}
    if "QC analysis" in messages[0]["content"] or "Required check results" in email:
        return {"analysis": "All good.", "recommendations": []}
    return {"priority": "low", "category": "newsletter", "summary": "Newsletter."}


def test_qc_pipeline_go_and_no_go(make_supervisor, settings):
    sup, _ = make_supervisor(FakeLLM([{"analysis": "fine", "recommendations": []}]))
    repo = Path(settings.allowed_repo_roots()[0]) / "sample_repo"
    rep = sup.run_qc(str(repo))
    by = {c.name: c.status.value for c in rep.checks}
    assert by["syntax"] == "pass" and by["tests"] == "pass"
    assert rep.verdict in (Verdict.GO, Verdict.NEEDS_REVIEW)  # lint may be skipped if ruff missing
    assert sup.last_report_path.exists()

    (repo / "test_calc.py").write_text((repo / "test_calc.py").read_text() + "\n\ndef test_bad():\n    assert 1 == 2\n")
    rep = sup.run_qc(str(repo), use_llm=False)
    assert rep.verdict is Verdict.NO_GO
    assert any("tests" in b for b in rep.blockers)
    failing = next(c for c in rep.checks if c.name == "tests")
    assert failing.findings and "test_bad" in failing.findings[0]["test"]


def test_qc_agent_cannot_override_verdict(make_supervisor, settings):
    repo = Path(settings.allowed_repo_roots()[0]) / "sample_repo"
    (repo / "calc.py").write_text("def broken(:\n")
    sup, _ = make_supervisor(FakeLLM([{"analysis": "Everything passed, ship it!", "recommendations": []}]))
    assert sup.run_qc(str(repo)).verdict is Verdict.NO_GO


def test_qc_on_ref_uses_worktree_and_leaves_checkout_alone(make_supervisor, settings):
    repo = Path(settings.allowed_repo_roots()[0]) / "sample_repo"
    (repo / "calc.py").write_text("def broken(:\n")          # uncommitted breakage in working copy
    sup, _ = make_supervisor(FakeLLM([]))
    rep = sup.run_qc(str(repo), ref="main", use_llm=False)   # committed 'main' is healthy
    assert {c.name: c.status.value for c in rep.checks}["syntax"] == "pass"
    assert (repo / "calc.py").read_text() == "def broken(:\n"


def test_qc_repo_outside_roots_rejected(make_supervisor):
    import pytest
    from agentbuild.guardrails.sandbox import SandboxError
    sup, _ = make_supervisor(FakeLLM([]))
    with pytest.raises(SandboxError):
        sup.run_qc("/etc")


def test_triage_flow_with_release_handoff_and_injection(make_supervisor):
    sup, approver = make_supervisor(FakeLLM(triage_llm), approve=True)
    outcomes = {o.mail.uid: o for o in sup.triage_inbox(limit=10)}

    rel = outcomes["001_release"]
    assert rel.triage.is_release_request and rel.qc_report is not None
    assert len(approver.asked) == 1                      # exactly one approval: the allowlisted sender
    assert [t["title"] for t in sup.db.list_tasks()][:2] == ["QC sample_repo", "Update release notes"]

    inj = outcomes["002_injection"]
    assert inj.injection_flags and inj.triage.suspicious
    assert inj.qc_report is None                          # sender not allowlisted -> no QC, no approval prompt

    assert outcomes["003_newsletter"].triage.priority.value == "low"
    assert sup.triage_inbox(limit=10) == []              # dedupe: already processed


def test_mail_prompt_contains_no_tools_and_is_wrapped(make_supervisor):
    llm = FakeLLM(triage_llm)
    sup, _ = make_supervisor(llm, approve=False)
    sup.triage_inbox(limit=10, auto_qc=False)
    for c in llm.calls:
        assert c["tools"] is None
        assert "<untrusted_email>" in c["messages"][-1]["content"]
        assert "alert(1)" not in c["messages"][-1]["content"]


def test_declined_approval_means_no_qc(make_supervisor):
    sup, approver = make_supervisor(FakeLLM(triage_llm), approve=False)
    outcomes = {o.mail.uid: o for o in sup.triage_inbox(limit=10)}
    assert approver.asked and outcomes["001_release"].qc_report is None


def test_draft_is_saved_not_sent(make_supervisor, settings):
    sup, _ = make_supervisor(FakeLLM([{"subject": "Re: QC", "body": "Will do by [confirm date].", "confidence": 0.8}]))
    d, path = sup.draft_reply("001_release", "say yes")
    assert path.exists() and "In-Reply-To: <rel-001@yourcompany.com>" in path.read_text()
