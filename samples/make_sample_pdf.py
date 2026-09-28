"""Generates samples/docs/release_policy.pdf. The last page is image-only (simulates a scan) to exercise OCR."""
from pathlib import Path

import pymupdf


def make_pdf(path: Path, pages: list[str], scanned: set[int] = frozenset()) -> Path:
    """pages: text per page. Page numbers (1-based) in `scanned` are rasterised: no text layer."""
    doc = pymupdf.open()
    for i, text in enumerate(pages, start=1):
        if i in scanned:
            tmp = pymupdf.open()
            tp = tmp.new_page()
            tp.insert_textbox(pymupdf.Rect(60, 60, 540, 780), text, fontsize=13, fontname="helv")
            pix = tp.get_pixmap(dpi=200)
            page = doc.new_page()
            page.insert_image(page.rect, pixmap=pix)
            tmp.close()
        else:
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(60, 60, 540, 780), text, fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()
    return path


PAGES = [
    """Acme Release Quality Policy - version 3.2

1. Purpose and scope
This policy defines the quality gates every service must pass before a production release.
It applies to all backend services, web applications and shared libraries.

Policy owner: Priya Raman, Release Manager.
Effective date: 1 July 2026. Reviewed every six months.""",

    """2. Quality gates

A release is blocked unless all of the following hold:
- Unit test pass rate is 100% on the release branch.
- Line coverage for new or changed code is at least 80%.
- Static analysis reports no critical or high severity findings.
- The secret scan is clean. No exceptions are granted for secrets.
- p95 latency does not regress by more than 5% compared with the previous release.

Medium severity findings may ship with a ticket that has a fix date within 30 days.""",

    """3. Release windows and code freeze

Code freeze begins every Thursday at 18:00 IST.
Standard releases run on Friday between 10:00 and 14:00 IST.
No releases on public holidays or on the last business day of a quarter.
Emergency changes outside the window follow the hotfix process in section 4.""",

    """4. Hotfix process

A hotfix needs two approvals, one of which must come from the on-call lead.
Every hotfix must include a regression test for the defect it fixes.
Hotfixes may bypass the code freeze but may never bypass the secret scan.
Roll back within 15 minutes if the error rate exceeds 2% after deployment.""",

    """5. Contacts and escalation

Release desk: release-desk@acme.example
Escalation contact: Arjun Mehta, VP Engineering
Response time for release blockers: 30 minutes during business hours.""",
]

if __name__ == "__main__":
    out = Path(__file__).parent / "docs" / "release_policy.pdf"
    out.parent.mkdir(exist_ok=True)
    make_pdf(out, PAGES, scanned={5})
    print(out)
