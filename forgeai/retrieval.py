"""Chunking + hybrid retrieval (BM25 + dense, fused with weighted RRF).

All knobs are fields of `RetrievalConfig`, which is serialised into the agent
definition. The optimizer changes these knobs as targeted fixes for
retrieval-miss failures."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np

from .documents import Document
from .llm import LLM

_STOP = set(
    """a an the and or of to in on for with by at from is are was were be been being this that these those it its
    as if then than so do does did can could should would may might will shall what which who whom how when where why
    i you he she we they me my your our their there here not no yes any all each per into about over under up out
    have has had also more most such only own same other some very just""".split()
)


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*", text.lower())
    return [t for t in toks if t not in _STOP and len(t) > 1 or t.isdigit()]


@dataclass
class ChunkingConfig:
    chunk_words: int = 180
    overlap_words: int = 40
    rows_per_chunk: int = 1  # table documents


@dataclass
class RetrievalConfig:
    top_k: int = 5
    bm25_weight: float = 0.5  # 0 = dense only, 1 = BM25 only
    query_expansion: bool = False
    neighbor_expansion: bool = False  # also include adjacent chunks of top hits
    min_dense_score: float = 0.0  # below this, chunks are flagged as weak evidence


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    meta: dict = field(default_factory=dict)

    def label(self) -> str:
        loc = self.meta.get("page") and f" p.{self.meta['page']}" or ""
        if "rows" in self.meta:
            loc = f" rows {self.meta['rows']}"
        return f"{self.doc_id}{loc}"


_NUMBERED = re.compile(r"^(?:#+\s*|(?:\d+(?:\.\d+)*\.?|[A-Z]\d+(?:\.\d+)*\.?|(?:Part|Section|Chapter|Annex|Appendix)\s+[A-Z0-9]+\s*[-:.]?)\s+)[A-Z(]")
_TITLE = re.compile(r"^[A-Z][A-Za-z0-9 ,&/()'\-]{2,70}$")
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\(?[a-z0-9]\))\s+")


def _is_heading(line: str, prev: str = ".", nxt: str = "") -> bool:
    line = line.strip()
    if not line or len(line.split()) > 10 or line[-1] in ".,;:" or len(line) > 80:
        return False
    if _NUMBERED.match(line) or line.startswith("#"):
        return True
    if line.isupper() and len(line) > 3:
        return True
    # unnumbered title-case line: must follow a finished sentence and precede a capitalised line
    return bool(_TITLE.match(line)) and (not prev.strip() or prev.strip()[-1] in ".:;!?") and (not nxt or nxt[:1].isupper())


def _segment(page: str) -> list[tuple[str | None, str]]:
    """Split page text into (heading_or_None, paragraph) units, line by line.

    PDF extractors emit one line per visual line with no blank lines between
    paragraphs, so headings are detected per line and short lines ending a
    sentence close a paragraph."""
    lines = [l.rstrip() for l in page.split("\n")]
    units: list[tuple[str | None, str]] = []
    cur: list[str] = []

    def flush():
        if cur:
            units.append((None, " ".join(x.strip() for x in cur)))
            cur.clear()

    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            flush()
            continue
        prev = lines[i - 1] if i else "."
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _is_heading(s, prev if prev.strip() else ".", nxt):
            flush()
            units.append((s.lstrip("# ").strip(), ""))
            continue
        if _BULLET.match(s):
            flush()
        cur.append(s)
        if s[-1] in ".:;" and len(s.split()) < 9:
            flush()
    flush()
    return units


def chunk_documents(docs: list[Document], cfg: ChunkingConfig) -> list[Chunk]:
    chunks: list[Chunk] = []
    for d in docs:
        if d.kind == "table":
            step = max(1, cfg.rows_per_chunk)
            for i in range(0, len(d.rows), step):
                rows = d.rows[i : i + step]
                body = "\n".join("; ".join(f"{k}: {v}" for k, v in r.items()) for r in rows)
                chunks.append(
                    Chunk(f"{d.doc_id}#r{i + 1}", d.doc_id, f"[{d.doc_id}]\n{body}", {"rows": f"{i + 1}-{i + len(rows)}"})
                )
            continue
        # text: walk pages/paragraphs, keep heading context, pack to size with overlap
        pages = d.pages or [d.text]
        units: list[tuple[int, str, str]] = []  # (page, heading, paragraph)
        heading = ""
        pending_head = ""
        for pno, page in enumerate(pages, 1):
            for head, para in _segment(page):
                if head is not None:
                    heading = head
                    pending_head = head
                    continue
                if pending_head:  # keep the heading words in the body so lexical search sees them
                    para = f"{pending_head}: {para}"
                    pending_head = ""
                units.append((pno, heading, para))
        state = {"buf": [], "page": 1, "head": "", "heads": [], "fresh": True, "n": 0}

        def size() -> int:
            return len(" ".join(state["buf"]).split())

        def flush():
            if not state["buf"]:
                return
            words = " ".join(state["buf"]).split()
            head = " / ".join(state["heads"])
            header = f"[{d.doc_id}" + (f" > {head}" if head else "") + "]"
            chunks.append(Chunk(f"{d.doc_id}#c{state['n']}", d.doc_id, header + "\n" + " ".join(words), {"page": state["page"], "heading": head}))
            state["n"] += 1
            tail = words[-cfg.overlap_words :] if cfg.overlap_words else []
            state["buf"] = [" ".join(tail)] if tail else []
            state["fresh"] = True  # only overlap left: next content decides the heading
            state["heads"] = state["heads"][-1:]

        def add(pno: int, head: str, text: str):
            if state["fresh"]:
                state["page"], state["head"], state["fresh"] = pno, head, False
            if head and head not in state["heads"]:
                state["heads"].append(head)
            state["buf"].append(text)

        for pno, head, para in units:
            words = para.split()
            while len(words) > cfg.chunk_words:  # very long paragraph: hard split
                if size() > cfg.overlap_words:
                    flush()
                take = max(cfg.chunk_words - size(), 20)
                add(pno, head, " ".join(words[:take]))
                words = words[take:]
                flush()
            if size() + len(words) > cfg.chunk_words and size() > cfg.overlap_words:
                flush()
            add(pno, head, " ".join(words))
        if state["buf"] and (not state["fresh"] or state["n"] == 0):
            flush()
    return chunks


class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(t) for t in docs_tokens]
        self.len = np.array([len(t) for t in docs_tokens], dtype=np.float32)
        self.avg = float(self.len.mean()) if len(self.len) else 1.0
        df = Counter(w for t in docs_tokens for w in set(t))
        N = len(docs_tokens)
        self.idf = {w: math.log(1 + (N - n + 0.5) / (n + 0.5)) for w, n in df.items()}

    def scores(self, q: list[str]) -> np.ndarray:
        out = np.zeros(len(self.tf), dtype=np.float32)
        for i, tf in enumerate(self.tf):
            s = 0.0
            for w in q:
                if w in tf:
                    f = tf[w]
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            out[i] = s
        return out


@dataclass
class Hit:
    chunk: Chunk
    score: float
    dense: float
    bm25: float
    rank: int


class Index:
    def __init__(self, docs: list[Document], chunking: ChunkingConfig, llm: LLM):
        self.docs = docs
        self.chunking = chunking
        self.llm = llm
        self.chunks = chunk_documents(docs, chunking)
        self.bm25 = BM25([tokenize(c.text) for c in self.chunks])
        self.emb = llm.embed([c.text for c in self.chunks], kind="document")
        self._pos = {c.chunk_id: i for i, c in enumerate(self.chunks)}

    def _rank(self, query: str, bm25_weight: float, n: int) -> list[tuple[int, float, float, float]]:
        qv = self.llm.embed([query], kind="query")[0]
        dense = self.emb @ qv
        sparse = self.bm25.scores(tokenize(query))
        d_rank = np.argsort(-dense)
        s_rank = np.argsort(-sparse)
        rrf = np.zeros(len(self.chunks), dtype=np.float32)
        K = 60.0
        for r, i in enumerate(d_rank):
            rrf[i] += (1 - bm25_weight) / (K + r + 1)
        for r, i in enumerate(s_rank):
            if sparse[i] > 0:
                rrf[i] += bm25_weight / (K + r + 1)
        order = np.argsort(-rrf)[:n]
        return [(int(i), float(rrf[i]), float(dense[i]), float(sparse[i])) for i in order]

    def search(self, query: str, cfg: RetrievalConfig, extra_queries: list[str] | None = None, k: int | None = None) -> list[Hit]:
        k = k or cfg.top_k
        queries = [query] + list(extra_queries or [])
        agg: dict[int, list[float]] = {}
        for qi, q in enumerate(queries):
            for r, (i, s, d, b) in enumerate(self._rank(q, cfg.bm25_weight, max(k * 3, 20))):
                w = 1.0 if qi == 0 else 0.6
                prev = agg.get(i, [0.0, -1.0, 0.0])
                agg[i] = [prev[0] + w * s, max(prev[1], d), max(prev[2], b)]
        order = sorted(agg.items(), key=lambda kv: -kv[1][0])[:k]
        hits = [Hit(self.chunks[i], v[0], v[1], v[2], r + 1) for r, (i, v) in enumerate(order)]
        if cfg.neighbor_expansion:
            seen = {h.chunk.chunk_id for h in hits}
            extra = []
            for h in hits[:2]:
                p = self._pos[h.chunk.chunk_id]
                for j in (p - 1, p + 1):
                    if 0 <= j < len(self.chunks) and self.chunks[j].doc_id == h.chunk.doc_id and self.chunks[j].chunk_id not in seen:
                        seen.add(self.chunks[j].chunk_id)
                        extra.append(Hit(self.chunks[j], h.score * 0.5, h.dense, 0.0, len(hits) + len(extra) + 1))
            hits += extra
        return hits

    def rank_of_doc(self, query: str, doc_id: str, cfg: RetrievalConfig, depth: int = 40, mode: str = "hybrid") -> int | None:
        """Diagnostic: rank (1-based) of the first chunk of `doc_id`, or None."""
        w = {"hybrid": cfg.bm25_weight, "bm25": 1.0, "dense": 0.0}[mode]
        for r, (i, *_rest) in enumerate(self._rank(query, w, depth)):
            if self.chunks[i].doc_id == doc_id:
                return r + 1
        return None


def config_dict(c) -> dict:
    return asdict(c)
