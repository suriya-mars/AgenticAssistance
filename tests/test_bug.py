import sys
import time
from email import policy
from email.parser import BytesParser
from email.utils import formatdate
from pathlib import Path

import pytest

from agentbuild.bug.service import BugService, WorkflowError
from agentbuild.bug.store import BugStore
from agentbuild.bug.templates import render_text
from agentbuild.bug.workflow import mentions_bug, normalize_bug_id
from agentbuild.core.approval import StaticApprover
from agentbuild.mail.drafts import LocalDraftSink
from agentbuild.mail.imap_source import LocalEmlSource

from conftest import ROOT, FakeLLM

sys.path.insert(0, str(ROOT / "samples"))
from make_asoc_pdf import FIXED_PAGES, PAGES as FAIL_PAGES  # noqa: E402
from make_sample_pdf import make_pdf  # noqa: E402

BUG = "BUG-12345"
SEC, TESTER, PM, KL = ("security-team@yourcompany.com", "tester@yourcompany.com",
                       "pm@yourcompany.com", "keyleader@yourcompany.com")


def fake_model(messages):
    """Plays every BUG agent. The ASoC reader is deliberately imperfect (misreads a count, invents an issue)."""
    system, task = messages[0]["content"], messages[-1]["content"]
    if "AppScan on Cloud (ASoC)" in system:
        fixed = "release-2.4.1" in task
        return {"application": "Payments API", "scan_name": "", "scan_date": "",
                "critical": 0, "high": 0 if fixed else 3, "medium": 3 if fixed else 5, "low": 4, "informational": 3,
                "top_issues": [] if fixed else [
                    {"issue_type": "SQL Injection", "severity": "high", "count": 1, "location": "/api/v2/orders"},
                    {"issue_type": "Remote Code Execution", "severity": "critical", "count": 1, "location": ""}],
                "summary": "Findings summarised."}
    if "classify a reply" in system:
        body = task.split("<untrusted_email>")[-1].lower()
        sig = {"kind": "info", "summary": "FYI.", "conditions": "", "requested_changes": "",
               "reply_in_thread": "on top of this mail" in body}
        if "not acceptable" in body or "fail" in body:
            sig |= {"kind": "rejection", "summary": "Rejected.", "requested_changes": "disable TLS 1.0/1.1"}
        elif "sign-off" in body or "signed off" in body:
            sig |= {"kind": "signoff", "summary": "Gives sign-off.",
                    "conditions": "track the TLS finding" if "provided" in body else ""}
        elif "approved" in body:
            sig |= {"kind": "approval", "summary": "Approves."}
        return sig
    if "ONE bug's quality process" in system:
        return {"answer": "Round 1 failed the ASoC gate; round 2 was rejected by security.",
                "sources": ["status", "mail:1", "mail:99"]}
    raise AssertionError(f"unexpected agent: {system[:80]}")


class Box:
    """A local mailbox; mails are dated `offset` seconds from now (after the current round started)."""
    def __init__(self, path: Path):
        self.path, self.n = path, 0
        path.mkdir()

    def add(self, sender, subject, body, offset=60) -> str:
        self.n += 1
        mid = f"<m{self.n}@test>"
        (self.path / f"{self.n:03d}.eml").write_text(
            f"From: {sender}\nTo: you@example.com\nSubject: {subject}\nMessage-ID: {mid}\n"
            f"Date: {formatdate(time.time() + offset, localtime=True)}\n\n{body}\n")
        return mid


@pytest.fixture
def fail_pdf(tmp_path):
    return make_pdf(tmp_path / "asoc_round1.pdf", FAIL_PAGES)


@pytest.fixture
def pass_pdf(tmp_path):
    return make_pdf(tmp_path / "asoc_fixed.pdf", FIXED_PAGES)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "inbox")


@pytest.fixture
def svc(make_supervisor, settings, box):
    def _make(approve=True):
        sup, _ = make_supervisor(FakeLLM(fake_model))
        sup.mail = LocalEmlSource(box.path)
        return BugService(sup, BugStore(sup.db.conn), LocalDraftSink(settings.path("drafts_dir") / "bugs"),
                          StaticApprover(approve))
    return _make


def _read(path: str):
    return BytesParser(policy=policy.default).parsebytes(Path(path.split("(copy: ")[-1].rstrip(")")).read_bytes())


# ---------------------------------------------------------------------------------------------------- basics
def test_bug_ids():
    pat = r"[A-Z][A-Z0-9_]*-\d+"
    assert normalize_bug_id(" 12345 ", pat, "BUG-") == "BUG-12345"
    assert normalize_bug_id("bug-7", pat, "BUG-") == "BUG-7"
    with pytest.raises(ValueError):
        normalize_bug_id("BUG 12; drop", pat, "BUG-")
    assert mentions_bug("BUG-12345", "Re: [BUG-12345] ok")
    assert not mentions_bug("BUG-12345", "Re: [BUG-123456] other bug")


def test_templates_only_fill_known_placeholders():
    text, missing = render_text("Hi {recipient_name}, json {\"a\": 1} {scan_date}{resubmission_note}",
                                {"recipient_name": "Kavya", "resubmission_note": ""})
    assert text == "Hi Kavya, json {\"a\": 1} {scan_date}" and missing == ["scan_date"]


def test_template_with_windows_line_endings(tmp_path):
    from agentbuild.bug.templates import render
    t = tmp_path / "t.md"
    t.write_bytes(b"Subject: [{bug_id}] hello\r\n\r\nHi {recipient_name},\r\nbody\r\n")
    r = render(t, {"bug_id": "BUG-1", "recipient_name": "Kavya"})
    assert r.subject == "[BUG-1] hello" and r.body == "Hi Kavya,\nbody\n" and not r.missing


def test_steps_cannot_run_out_of_order(svc):
    s = svc()
    s.create(BUG, "Payments API order lookup fix")
    with pytest.raises(WorkflowError, match="Finish first"):
        s.draft(BUG, "tester")
    with pytest.raises(WorkflowError):
        s.create("12345", "duplicate")


# ---------------------------------------------------------------------------------------------------- ASoC gate
def test_failed_scan_blocks_the_bug(svc, fail_pdf):
    s = svc()
    s.create(BUG, "x")
    sc = s.analyze_scan(BUG, str(fail_pdf))
    assert sc["counts"] == {"critical": 0, "high": 2, "medium": 5, "low": 4, "informational": 3}
    assert sc["gate"] == "FAIL" and "2 high" in sc["gate_reason"]
    assert any("model read 3" in w for w in sc["warnings"])
    assert any("Remote Code Execution" in w for w in sc["warnings"])
    assert sc["injection_flags"]
    st = s.status(BUG)
    assert st["blocked"]["by"] == "asoc_gate" and "restart" in st["next"][0]["hint"]
    with pytest.raises(WorkflowError, match="BLOCKED"):
        s.draft(BUG, "security")
    with pytest.raises(WorkflowError, match="BLOCKED"):
        s.analyze_scan(BUG, str(fail_pdf))          # must restart, not just rescan


def test_strict_gate_fails_on_any_finding(svc, pass_pdf):
    s = svc()
    s.cfg["security_gate"] = {"critical": 0, "high": 0, "medium": 0, "low": 0, "informational": 0}
    s.create(BUG, "x")
    sc = s.analyze_scan(BUG, str(pass_pdf))
    assert sc["gate"] == "FAIL" and "3 medium" in sc["gate_reason"]


def test_model_that_reads_nothing_still_gets_correct_scan(svc, fail_pdf):
    """What qwen3:4b actually did once: only a summary, every other field empty."""
    s = svc()
    lazy = {"application": "", "scan_name": "", "scan_date": "", "critical": 0, "high": 0, "medium": 0,
            "low": 0, "informational": 0, "top_issues": [], "summary": "Some issues were found."}
    s.sup.llm.script = lambda m: lazy if "AppScan" in m[0]["content"] else fake_model(m)
    s.create(BUG, "x")
    sc = s.analyze_scan(BUG, str(fail_pdf))
    assert sc["counts"]["high"] == 2 and sc["gate"] == "FAIL" and len(sc["issues"]) == 6
    assert (sc["application"], sc["scan_date"]) == ("Payments API", "2026-09-20")
    assert sc["warnings"] == ["model did not read the counts; used the report's summary text"]


# ---------------------------------------------------------------------------------------------------- full flow
def test_fail_restart_security_rejection_thread_and_complete(svc, fail_pdf, pass_pdf, box):
    s = svc()
    s.create("12345", "Payments API order lookup fix")

    # Round 1: ASoC finds high issues -> blocked -> fix -> restart with the same id.
    s.analyze_scan(BUG, str(fail_pdf))
    bug = s.restart(BUG, "parameterized order query, encoded receipt view")
    assert bug.bug_id == BUG and bug.data["round"] == 2 and not bug.steps

    # Round 2: scan passes, but the security team must still approve - and they reject.
    assert s.analyze_scan(BUG, str(pass_pdf))["gate"] == "PASS"
    [sec] = s.draft(BUG, "security")
    assert not sec.in_thread                                      # no earlier security mail exists
    assert "re-submission #1 after fixing the earlier findings: parameterized order query" in sec.rendered.body
    with pytest.raises(WorkflowError, match="Security team approved"):
        s.draft(BUG, "tester")                                    # passing scan is not enough
    round2_sec_id = s.store.mails(BUG)[-1]["message_id"]
    rej = box.add(SEC, "Re: [BUG-12345] ASoC security scan review",
                  "TLS 1.0 is still enabled. Not acceptable. Please fix it and send the update on top of this mail.")
    old_approval = box.add(TESTER, "Re: [BUG-12345] Testing request", "Approved.", offset=-3600)  # before round 2
    items = {i.mail.message_id: i for i in s.sync(BUG)}
    assert items[rej].action == "blocked_by_security"
    assert items[old_approval].action == "previous_round"
    st = s.status(BUG)
    assert st["blocked"]["by"] == "security_team" and st["thread_requested"] == {"security": True}

    # Round 3: fix TLS, rescan, and the security mail goes ON TOP of the existing thread.
    s.restart(BUG, "disabled TLS 1.0 and 1.1")
    s.analyze_scan(BUG, str(pass_pdf))
    [sec3] = s.draft(BUG, "security")
    assert sec3.in_thread
    eml = _read(sec3.location)
    assert eml["Subject"] == "Re: [BUG-12345] ASoC security scan review - Payments API order lookup fix"
    assert eml["In-Reply-To"] == rej and round2_sec_id in eml["References"]
    body = eml.get_body(("plain",)).get_content()
    assert "re-submission #2" in body and "> TLS 1.0 is still enabled. Not acceptable." in body
    assert eml.get_all("Content-Disposition") or any(p.get_filename() for p in eml.iter_attachments())

    items = {i.mail.message_id: i for i in s.sync(BUG)}
    assert items[rej].action in ("seen", "previous_round")      # round-2 rejection does not block round 3

    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Reviewed the fixed scan. Approved from security.")
    box.add(TESTER, "Re: [BUG-12345] Testing request", "Scope approved.")
    s.sync(BUG)
    assert "security_approval" in s.get(BUG).done and "tester_approval" not in s.get(BUG).done  # tester mail not sent

    s.draft(BUG, "tester")
    box.add(TESTER, "Re: [BUG-12345] Testing request", "Testing done, sign-off given, provided TLS is tracked.")
    box.add("bot@gmail.com", "Re: [BUG-12345] Sign-off required", "Signed off.")
    items = s.sync(BUG)
    assert {"tester_approval", "tester_signoff"} <= s.get(BUG).done       # scope-approval mail + sign-off mail
    assert any(i.action == "ignored_sender" for i in items)

    pm, kl = s.draft(BUG, "signoff")
    assert "Tester sign-off: tester@yourcompany.com on" in pm.rendered.body
    box.add(PM, "Re: [BUG-12345] Sign-off required", "Signed off from project side.")
    box.add(KL, "Re: [BUG-12345] Sign-off required", "You have my sign-off.")
    s.sync(BUG)
    st = s.status(BUG)
    assert st["complete"] and st["round"] == 3 and len(st["history"]) == 2
    assert [h["blocked"]["by"] for h in st["history"]] == ["asoc_gate", "security_team"]
    assert all(c["tools"] is None for c in s.sup.llm.calls)


# ---------------------------------------------------------------------------------------------------- human control
def _ready_for_security_reply(s, pass_pdf):
    s.create(BUG, "x")
    s.analyze_scan(BUG, str(pass_pdf))
    s.draft(BUG, "security")


def test_declined_security_block_keeps_process_open(svc, pass_pdf, box):
    s = svc()
    _ready_for_security_reply(s, pass_pdf)
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Not acceptable yet.")
    s.approver.answer = False
    [item] = s.sync(BUG)
    assert item.action == "rejection" and not s.status(BUG)["blocked"]


def test_declined_confirmation_leaves_step_open(svc, pass_pdf, box):
    s = svc()
    _ready_for_security_reply(s, pass_pdf)
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Approved.")
    s.approver.answer = False
    [item] = s.sync(BUG)
    assert item.action == "dismissed" and "security_approval" not in s.get(BUG).done
    s.approver.answer = True
    s.mark(BUG, "security_approval", note="approved in the review meeting")
    assert "security_approval" in s.get(BUG).done


def test_declined_draft_is_not_saved(svc, pass_pdf, settings):
    s = svc(approve=False)
    s.create(BUG, "x")
    s.analyze_scan(BUG, str(pass_pdf))
    [r] = s.draft(BUG, "security")
    assert not r.saved and "security_mail" not in s.get(BUG).done
    assert not list((settings.path("drafts_dir") / "bugs").glob("*.eml"))


def test_tester_rejection_is_recorded_without_blocking(svc, pass_pdf, box):
    s = svc()
    _ready_for_security_reply(s, pass_pdf)
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Approved.")
    s.sync(BUG)
    s.draft(BUG, "tester")
    box.add(TESTER, "Re: [BUG-12345] Testing request", "3 regression tests fail.")
    [item] = [i for i in s.sync(BUG) if i.action != "seen"]
    assert item.action == "rejection" and s.status(BUG)["rejections"] and not s.status(BUG)["blocked"]


def test_ask_sees_rounds_and_only_this_bug(svc, fail_pdf, pass_pdf, box):
    s = svc()
    s.create(BUG, "Payments API order lookup fix")
    s.analyze_scan(BUG, str(fail_pdf))
    s.restart(BUG, "fixed SQLi and XSS")
    s.analyze_scan(BUG, str(pass_pdf))
    s.draft(BUG, "security")
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Not acceptable.")
    box.add(TESTER, "Re: [BUG-123456] other bug", "Approved.")
    s.sync(BUG)
    ans, unknown = s.ask(BUG, "What happened so far?")
    assert ans.sources == ["status", "mail:1"] and unknown == ["mail:99"]
    prompt = s.sup.llm.calls[-1]["messages"][-1]["content"]
    assert "Earlier round 1: blocked by asoc_gate" in prompt and "BLOCKED: YES - by security_team" in prompt
    assert "BUG-123456" not in prompt


# ---------------------------------------------------------------------------------------------------- crash safety
class CrashOnAsk(StaticApprover):
    """Says yes, except it 'crashes' (Ctrl-C / power loss) at the n-th question."""
    def __init__(self, crash_at: int):
        super().__init__(True)
        self.crash_at = crash_at

    def approve(self, action, detail):
        if len(self.asked) + 1 == self.crash_at:
            self.asked.append((action, detail))
            raise KeyboardInterrupt
        return super().approve(action, detail)


def test_crash_between_signoff_drafts_does_not_duplicate(svc, pass_pdf, box, settings):
    s = svc()
    _ready_for_security_reply(s, pass_pdf)
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Approved.")
    s.sync(BUG)
    s.draft(BUG, "tester")
    box.add(TESTER, "Re: [BUG-12345] Testing request", "Scope approved. Testing done, sign-off given.")
    s.sync(BUG)
    s.approver = CrashOnAsk(crash_at=2)                 # PM draft saved, crash at the key leader prompt
    with pytest.raises(KeyboardInterrupt):
        s.draft(BUG, "signoff")
    assert "signoff_mails" not in s.get(BUG).done
    s.approver = StaticApprover(True)                   # user just runs the same command again
    pm, kl = s.draft(BUG, "signoff")
    assert "not duplicated" in pm.location and kl.saved and "not duplicated" not in kl.location
    assert len(s.approver.asked) == 1                   # only asked about the key leader mail
    assert "signoff_mails" in s.get(BUG).done
    assert len(list((settings.path("drafts_dir") / "bugs").glob("*pm_signoff*"))) == 1


def test_crash_at_security_rejection_prompt_asks_again(svc, pass_pdf, box):
    s = svc()
    _ready_for_security_reply(s, pass_pdf)
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Not acceptable.")
    s.approver = CrashOnAsk(crash_at=1)
    with pytest.raises(KeyboardInterrupt):
        s.sync(BUG)
    assert not s.status(BUG)["blocked"]
    s.approver = StaticApprover(True)
    [item] = s.sync(BUG)
    assert item.action == "blocked_by_security" and s.status(BUG)["blocked"]


def test_failed_scan_stays_blocked_even_without_block_flag(svc, fail_pdf):
    s = svc()
    s.create(BUG, "x")
    s.analyze_scan(BUG, str(fail_pdf))
    bug = s.get(BUG)
    bug.data.pop("blocked")                             # as if the process died before recording the block
    s.store.update(bug)
    with pytest.raises(WorkflowError, match="BLOCKED"):
        s.draft(BUG, "security")


def test_crash_mid_sync_resumes_from_cached_classification(svc, pass_pdf, box):
    s = svc()
    _ready_for_security_reply(s, pass_pdf)
    box.add(SEC, "Re: [BUG-12345] ASoC security scan review", "Approved.")
    s.approver = CrashOnAsk(crash_at=1)
    with pytest.raises(KeyboardInterrupt):
        s.sync(BUG)
    calls = len(s.sup.llm.calls)
    s.approver = StaticApprover(True)
    s.sync(BUG)
    assert "security_approval" in s.get(BUG).done
    assert len(s.sup.llm.calls) == calls                # the mail was not sent to the model a second time
