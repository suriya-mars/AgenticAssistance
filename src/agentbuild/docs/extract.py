"""PDF -> per-page text. Text layer via PyMuPDF; pages without one fall back to local Tesseract OCR."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from agentbuild.guardrails import sandbox


class PdfError(Exception):
    pass


@dataclass
class Page:
    number: int      # 1-based, as printed in citations
    text: str
    ocr: bool = False


def tesseract_bin() -> str | None:
    """Tesseract location: $AGENT_TESSERACT, then PATH, then the usual Windows install folders."""
    if (env := os.environ.get("AGENT_TESSERACT")) and Path(env).is_file():
        return env
    if found := shutil.which("tesseract"):
        return found
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA", "") + "\\Programs"):
        if base and (p := Path(base) / "Tesseract-OCR" / "tesseract.exe").is_file():
            return str(p)
    return None


def ocr_available() -> bool:
    return tesseract_bin() is not None


def ocr_languages() -> set[str]:
    if not ocr_available():
        return set()
    r = sandbox.run([tesseract_bin(), "--list-langs"], cwd=Path(tempfile.gettempdir()), timeout_s=15)
    lines = (r.stdout + r.stderr).splitlines()
    return {l.strip() for l in lines if l.strip() and " " not in l.strip() and not l.endswith(":")}


def ocr_problem(lang: str) -> str | None:
    """None if OCR can run for `lang` (e.g. 'eng+tam'), else a fix-it message."""
    if not ocr_available():
        return ("tesseract is not installed (optional, only for scanned PDFs). Linux: install tesseract + "
                "tesseract-data-eng; Windows: UB Mannheim Tesseract installer, or set AGENT_TESSERACT to tesseract.exe")
    missing = [l for l in lang.split("+") if l not in ocr_languages()]
    if missing:
        pkgs = " ".join(f"tesseract-data-{m}" for m in missing)
        return f"tesseract language pack missing: {', '.join(missing)} (Arch: sudo pacman -S {pkgs})"
    return None


def _ocr_page(page: pymupdf.Page, lang: str, dpi: int, timeout_s: int) -> str:
    with tempfile.TemporaryDirectory(prefix="agentocr-") as tmp:
        png = Path(tmp) / "page.png"
        page.get_pixmap(dpi=dpi).save(png)
        r = sandbox.run([tesseract_bin(), str(png), "stdout", "-l", lang], cwd=Path(tmp), timeout_s=timeout_s)
        return r.stdout if r.exit_code == 0 else ""


def extract_pages(path: Path, *, ocr: bool = True, ocr_lang: str = "eng", max_pages: int = 500,
                  max_file_mb: float = 50, min_chars_for_text: int = 25, ocr_timeout_s: int = 90) -> list[Page]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PdfError(f"file not found: {path}")
    if path.stat().st_size > max_file_mb * 1024 * 1024:
        raise PdfError(f"file is larger than {max_file_mb} MB: {path.name}")
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise PdfError(f"cannot open PDF {path.name}: {e}") from e
    with doc:
        if not doc.is_pdf:
            raise PdfError(f"not a PDF: {path.name}")
        if doc.needs_pass:
            raise PdfError(f"PDF is password-protected: {path.name}")
        if doc.page_count > max_pages:
            raise PdfError(f"PDF has {doc.page_count} pages; limit is {max_pages}")
        can_ocr = ocr and ocr_problem(ocr_lang) is None
        pages = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text", sort=True).strip()
            used_ocr = False
            if len(text) < min_chars_for_text and can_ocr:
                ocr_text = _ocr_page(page, ocr_lang, 200, ocr_timeout_s).strip()
                if len(ocr_text) > len(text):
                    text, used_ocr = ocr_text, True
            pages.append(Page(i, text, used_ocr))
    if not any(p.text for p in pages):
        problem = ocr_problem(ocr_lang) if ocr else "OCR is disabled (docs.ocr: false)"
        raise PdfError(f"no extractable text in {path.name}; looks scanned" + (f" and {problem}" if problem else ""))
    return pages
