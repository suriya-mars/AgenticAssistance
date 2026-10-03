"""Documents (PDFs, ASoC reports): add, list, search, ask with verified citations, remove."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from agentbuild.docs.store import DocMeta

from agentbuild_mcp.approvals import FixedApprover
from agentbuild_mcp.guards import UNTRUSTED_NOTE, resolve_document, untrusted
from agentbuild_mcp.runtime import Runtime, run_blocking
from agentbuild_mcp.tools._common import CONFIRM_HINT, annotations


def _meta(m: DocMeta) -> dict[str, Any]:
    return {"doc_id": m.doc_id, "name": m.name, "pages": m.pages, "passages": m.chunks, "chars": m.chars,
            "ocr_pages": m.ocr_pages, "pages_without_text": m.empty_pages}


def _doc(rt: Runtime, ref: str) -> DocMeta:
    """A document id, part of its name, or a path (added on first use, if the path is in an allowed folder)."""
    if Path(ref).suffix.lower() == ".pdf" or "/" in ref or "\\" in ref:
        return rt.sup.ingest_pdf(str(resolve_document(rt.cfg, ref)))[0]
    return rt.sup.docs.resolve(ref)


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=annotations("Add a document", idempotent=True))
    async def docs_add(path: str, ctx: Context, force: bool = False) -> dict[str, Any]:
        """Extract and index a PDF so it can be searched and questioned. `path` is relative to the AgentBuild
        folder (e.g. files/report.pdf) and must be inside an allowed folder. Scanned pages are OCR'd.
        Adding the same file again is instant (force=true re-extracts)."""

        def work(rt: Runtime) -> dict[str, Any]:
            meta, new = rt.sup.ingest_pdf(str(resolve_document(rt.cfg, path)), force=force)
            return _meta(meta) | {"newly_added": new}

        return await run_blocking(ctx, work, label="reading the document")

    @mcp.tool(annotations=annotations("List documents", read_only=True))
    async def docs_list(ctx: Context) -> dict[str, Any]:
        """All documents that have been added, newest first."""
        return await run_blocking(ctx, lambda rt: {"documents": [_meta(m) for m in rt.sup.docs.list()]})

    @mcp.tool(annotations=annotations("Search a document", read_only=True))
    async def docs_search(doc: str, query: str, ctx: Context, k: int = 5) -> dict[str, Any]:
        """Return the passages of a document that best match `query` (keyword search), with page numbers.
        `doc` is a document id, part of its name, or a path. Use this when you want to read the text yourself;
        use docs_ask to get an answer with checked quotes from the local model."""

        def work(rt: Runtime) -> dict[str, Any]:
            meta = _doc(rt, doc)
            hits = rt.sup.docs.search(meta.doc_id, query, k=max(1, min(k, 20)))
            limit = rt.cfg.max_text_chars // max(1, len(hits) or 1)
            return {"doc": _meta(meta),
                    "passages": [{"page": c.page, "score": round(score, 3),
                                  "text": untrusted(c.text, "document", limit)} for c, score in hits],
                    "note": UNTRUSTED_NOTE}

        return await run_blocking(ctx, work)

    @mcp.tool(annotations=annotations("Ask a document", read_only=True))
    async def docs_ask(doc: str, question: str, ctx: Context) -> dict[str, Any]:
        """Answer a question about a document with the local model. Every quote it cites is checked against
        the real page text: verified, page_corrected, approximate (all numbers match) or unverified.
        If `grounded` is false, no quote could be found in the document: treat the answer with care."""

        def work(rt: Runtime) -> dict[str, Any]:
            meta = _doc(rt, doc)
            r = rt.sup.ask_pdf(meta, question)
            read = "full text" if r.context and r.context.mode == "full" else \
                f"passages from pages {r.context.pages}" if r.context else "nothing"
            return {"doc": meta.name, "answer": r.answer, "found": r.found, "grounded": r.grounded,
                    "citations": [{"page": c.page, "quote": c.quote, "status": c.status,
                                   "model_said_page": c.claimed_page} for c in r.citations],
                    "read": read}

        return await run_blocking(ctx, work, label="the local model is reading")

    @mcp.tool(annotations=annotations("Remove a document", destructive=True))
    async def docs_remove(doc: str, ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Remove a document's extracted text and index (the original file is not touched).
        Call with confirm=false first to see what would be removed."""

        def work(rt: Runtime) -> dict[str, Any]:
            meta = rt.sup.docs.resolve(doc)
            if not confirm:
                return {"mode": "preview", "would_remove": _meta(meta), "hint": CONFIRM_HINT}
            rt.sup.docs.remove(meta.doc_id)
            return {"mode": "done", "removed": _meta(meta)}

        return await run_blocking(ctx, work, approver=FixedApprover(confirm))
