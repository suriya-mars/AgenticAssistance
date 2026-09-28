"""Document store: page-aware chunks on disk + BM25 retrieval (no embedding model needed)."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agentbuild.docs.extract import Page

_WORD = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_STOP = set("""a an and are as at be by for from has have in is it its of on or that the this to was were
will with which what when where who how why do does did can could should would i you we they he she
not no yes if then than so but about into over under there their them our your my me""".split())


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‐-―\-]", "-", text)).strip().lower()


@dataclass
class Chunk:
    id: int
    page: int
    text: str


@dataclass
class DocMeta:
    doc_id: str
    name: str
    source: str
    pages: int
    chunks: int
    chars: int
    ocr_pages: list[int]
    added_at: float
    empty_pages: list[int] = field(default_factory=list)  # no text even after OCR (or OCR unavailable)


def chunk_pages(pages: list[Page], size: int = 900, overlap: int = 150) -> list[Chunk]:
    """Split each page into ~size-char chunks on paragraph/line boundaries. Chunks never span
    pages, so every chunk has exactly one page number to cite."""
    chunks: list[Chunk] = []
    for p in pages:
        parts = [s.strip() for s in re.split(r"\n\s*\n|\n", p.text) if s.strip()]
        buf = ""
        for part in parts:
            while len(part) > size:  # very long paragraph: hard split
                head, part = part[:size], part[size - overlap:]
                if buf:
                    chunks.append(Chunk(len(chunks), p.number, buf))
                    buf = ""
                chunks.append(Chunk(len(chunks), p.number, head))
            if len(buf) + len(part) + 1 > size and buf:
                chunks.append(Chunk(len(chunks), p.number, buf))
                buf = buf[-overlap:].split(" ", 1)[-1] + " " if overlap else ""
            buf += part + "\n"
        if buf.strip():
            chunks.append(Chunk(len(chunks), p.number, buf.strip()))
    return chunks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = sum(self.len) / max(len(docs), 1)
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for q in query:
                f = tf.get(q)
                if f:
                    s += self.idf[q] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


class DocStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- ingest
    def add(self, path: Path, pages: list[Page], chunk_size: int = 900) -> DocMeta:
        path = Path(path).expanduser().resolve()
        doc_id = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        chunks = chunk_pages(pages, size=chunk_size)
        meta = DocMeta(doc_id, path.name, str(path), len(pages), len(chunks),
                       sum(len(p.text) for p in pages), [p.number for p in pages if p.ocr], time.time(),
                       [p.number for p in pages if not p.text])
        d = self.root / doc_id
        d.mkdir(exist_ok=True)
        (d / "meta.json").write_text(json.dumps(asdict(meta), indent=1))
        (d / "pages.json").write_text(json.dumps([asdict(p) for p in pages]))
        (d / "chunks.json").write_text(json.dumps([asdict(c) for c in chunks]))
        return meta

    def exists_for(self, path: Path) -> DocMeta | None:
        doc_id = hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()[:12]
        return self.meta(doc_id) if (self.root / doc_id).is_dir() else None

    # ----------------------------------------------------------------- query
    def list(self) -> list[DocMeta]:
        metas = [DocMeta(**json.loads(p.read_text())) for p in self.root.glob("*/meta.json")]
        return sorted(metas, key=lambda m: m.added_at, reverse=True)

    def meta(self, doc_id: str) -> DocMeta:
        return DocMeta(**json.loads((self.root / doc_id / "meta.json").read_text()))

    def resolve(self, ref: str) -> DocMeta:
        """Accept a doc id, id prefix, or (case-insensitive) file-name substring."""
        docs = self.list()
        hits = [m for m in docs if m.doc_id.startswith(ref)] or \
               [m for m in docs if ref.lower() in m.name.lower()]
        if len(hits) != 1:
            names = ", ".join(f"{m.doc_id} ({m.name})" for m in hits) or "none"
            raise KeyError(f"'{ref}' matches {len(hits)} documents: {names}")
        return hits[0]

    def pages(self, doc_id: str) -> list[Page]:
        return [Page(**p) for p in json.loads((self.root / doc_id / "pages.json").read_text())]

    def chunks(self, doc_id: str) -> list[Chunk]:
        return [Chunk(**c) for c in json.loads((self.root / doc_id / "chunks.json").read_text())]

    def search(self, doc_id: str, query: str, k: int = 6) -> list[tuple[Chunk, float]]:
        chunks = self.chunks(doc_id)
        bm = BM25([tokenize(c.text) for c in chunks])
        scored = sorted(zip(chunks, bm.scores(tokenize(query))), key=lambda x: x[1], reverse=True)
        return [(c, s) for c, s in scored[:k] if s > 0]

    def remove(self, doc_id: str) -> None:
        shutil.rmtree(self.root / doc_id, ignore_errors=True)
