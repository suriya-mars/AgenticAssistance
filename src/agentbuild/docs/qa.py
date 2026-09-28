"""Context building for PDF Q&A and citation verification (grounding check)."""
from __future__ import annotations

from dataclasses import dataclass, field

from agentbuild.docs.extract import Page
from agentbuild.docs.store import DocMeta, DocStore, normalize, tokenize


@dataclass
class DocContext:
    text: str
    pages: list[int]
    mode: str          # "full" (whole document fits) | "retrieved" (BM25 top passages)


def build_context(store: DocStore, meta: DocMeta, question: str, prev_question: str = "",
                  budget_chars: int = 12000, top_k: int = 6) -> DocContext:
    if meta.chars <= budget_chars:
        pages = store.pages(meta.doc_id)
        body = "\n\n".join(f"[page {p.number}]\n{p.text}" for p in pages if p.text)
        return DocContext(body, [p.number for p in pages if p.text], "full")

    # Follow-up questions ("and what about hotfixes?") borrow terms from the previous question.
    hits = store.search(meta.doc_id, f"{question} {prev_question}".strip(), k=top_k)
    if not hits:
        return DocContext("", [], "retrieved")
    picked, used = [], 0
    for chunk, _ in hits:
        if used + len(chunk.text) > budget_chars:
            continue
        picked.append(chunk)
        used += len(chunk.text)
    picked.sort(key=lambda c: c.id)  # reading order
    body = "\n\n".join(f"[page {c.page}]\n{c.text}" for c in picked)
    return DocContext(body, sorted({c.page for c in picked}), "retrieved")


@dataclass
class CheckedCitation:
    page: int
    quote: str
    status: str        # exact | page_corrected | approximate | unverified
    claimed_page: int | None = None


@dataclass
class QAResult:
    answer: str
    found: bool
    citations: list[CheckedCitation] = field(default_factory=list)
    context: DocContext | None = None

    @property
    def grounded(self) -> bool:
        return any(c.status != "unverified" for c in self.citations)


def check_citations(citations: list[tuple[int, str]], pages: list[Page]) -> list[CheckedCitation]:
    """A quote counts only if it really appears in the document. Wrong page numbers are corrected."""
    norm = {p.number: normalize(p.text) for p in pages}
    toks = {p.number: set(tokenize(p.text)) for p in pages}
    out = []
    for page, quote in citations:
        q = normalize(quote).strip(" .\"'“”…")
        if len(q) < 3:
            out.append(CheckedCitation(page, quote, "unverified"))
            continue
        if q in norm.get(page, ""):
            out.append(CheckedCitation(page, quote, "exact"))
            continue
        other = next((n for n, t in norm.items() if q in t), None)
        if other is not None:
            out.append(CheckedCitation(other, quote, "page_corrected", claimed_page=page))
            continue
        # Close paraphrase: every number must appear on the page verbatim, and >= 80% of the words.
        qt = tokenize(quote)
        pt = toks.get(page, set())
        numbers_ok = all(t in pt for t in qt if any(ch.isdigit() for ch in t))
        if len(qt) >= 4 and numbers_ok and sum(t in pt for t in qt) / len(qt) >= 0.8:
            out.append(CheckedCitation(page, quote, "approximate"))
            continue
        out.append(CheckedCitation(page, quote, "unverified"))
    return out
