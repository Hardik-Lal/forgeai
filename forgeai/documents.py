"""Load an arbitrary document folder (PDF, DOCX, TXT, MD, CSV, JSON) and profile it.

The factory must work on folders it has never seen, so loading is
format-driven and never assumes a particular domain."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".docx"}


@dataclass
class Document:
    doc_id: str  # file name, used as the citation source
    path: str
    kind: str  # "text" | "table"
    text: str
    pages: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    sha: str = ""

    @property
    def n_words(self) -> int:
        return len(self.text.split())


def _clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"-\n(?=[a-z])", "", text)  # de-hyphenate line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_pdf(p: Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(p))
    return [_clean(page.extract_text() or "") for page in reader.pages]


def _load_docx(p: Path) -> str:
    import docx  # python-docx

    d = docx.Document(str(p))
    parts = []
    for para in d.paragraphs:
        t = para.text.strip()
        if not t:
            continue
        if para.style is not None and para.style.name.lower().startswith("heading"):
            parts.append(f"\n## {t}")
        else:
            parts.append(t)
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return _clean("\n".join(parts))


def _load_table(p: Path) -> tuple[list[str], list[dict]]:
    raw = p.read_bytes().decode("utf-8-sig", errors="replace")
    delim = "\t" if p.suffix.lower() == ".tsv" else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(raw[:4000], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items() if k is not None} for r in reader]
    cols = [c.strip() for c in (reader.fieldnames or [])]
    return cols, rows


def load_folder(folder: str | Path) -> list[Document]:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"document folder not found: {folder}")
    docs: list[Document] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED or p.name.startswith("."):
            continue
        sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        rel = str(p.relative_to(folder))
        suf = p.suffix.lower()
        try:
            if suf == ".pdf":
                pages = _load_pdf(p)
                docs.append(Document(rel, str(p), "text", "\n\n".join(pages), pages=pages, sha=sha))
            elif suf in (".csv", ".tsv"):
                cols, rows = _load_table(p)
                text = "\n".join("; ".join(f"{k}: {v}" for k, v in r.items()) for r in rows)
                docs.append(Document(rel, str(p), "table", text, columns=cols, rows=rows, sha=sha))
            elif suf == ".docx":
                docs.append(Document(rel, str(p), "text", _load_docx(p), sha=sha))
            elif suf == ".json":
                data = json.loads(p.read_text())
                if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
                    cols = sorted({k for x in data for k in x})
                    rows = [{k: str(x.get(k, "")) for k in cols} for x in data]
                    text = "\n".join("; ".join(f"{k}: {v}" for k, v in r.items()) for r in rows)
                    docs.append(Document(rel, str(p), "table", text, columns=cols, rows=rows, sha=sha))
                else:
                    docs.append(Document(rel, str(p), "text", json.dumps(data, indent=1), sha=sha))
            else:
                docs.append(Document(rel, str(p), "text", _clean(p.read_text(errors="replace")), sha=sha))
        except Exception as e:  # a corrupt file must not kill an unattended run
            print(f"[documents] WARNING: could not read {rel}: {e}")
    docs = [d for d in docs if d.text.strip()]
    if not docs:
        raise ValueError(f"no readable documents in {folder}")
    return docs


def profile(docs: list[Document], sample_chars: int = 700) -> list[dict]:
    """Compact description of each document for prompts (no full text)."""
    out = []
    for d in docs:
        item = {"doc_id": d.doc_id, "kind": d.kind, "words": d.n_words}
        if d.kind == "table":
            item["columns"] = d.columns
            item["n_rows"] = len(d.rows)
            item["sample_rows"] = d.rows[:3]
        else:
            heads = re.findall(r"^(?:#+\s*)?((?:\d+(?:\.\d+)*\.?\s+)?[A-Z][^\n]{3,70})$", d.text, flags=re.M)
            item["headings"] = [h.strip() for h in heads if len(h.split()) <= 10][:25]
            item["opening"] = d.text[:sample_chars]
        out.append(item)
    return out


def profile_text(docs: list[Document]) -> str:
    lines = []
    for p in profile(docs):
        if p["kind"] == "table":
            lines.append(
                f"- {p['doc_id']} (table, {p['n_rows']} rows) columns: {', '.join(p['columns'])}\n"
                f"  sample row: {json.dumps(p['sample_rows'][0]) if p['sample_rows'] else '-'}"
            )
        else:
            heads = "; ".join(p["headings"][:15]) or "(no headings detected)"
            lines.append(
                f"- {p['doc_id']} (text, {p['words']} words)\n  headings: {heads}\n"
                f"  opening: {p['opening'][:400].replace(chr(10), ' ')}"
            )
    return "\n".join(lines)


def corpus_fingerprint(docs: list[Document]) -> dict[str, str]:
    # hash the EXTRACTED TEXT, not the file bytes: re-exporting an unchanged PDF changes its bytes (timestamps) but not its content
    return {d.doc_id: hashlib.sha256(d.text.encode()).hexdigest()[:16] for d in docs}
