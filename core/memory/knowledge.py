"""Approved knowledge base: parse versioned markdown docs and index them for retrieval.

Doc format::

    ---
    id: kyc_faq
    version: 1
    title: KYC FAQ
    scopes: [kyc_faq]
    review_status: DRAFT_UNREVIEWED
    ---
    ## Accepted documents {#accepted_docs}
    ...

Every section needs a stable ``{#id}``. A chunk is cited as ``kb:<doc>#<section>@v<version>``.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

SECTION_RE = re.compile(r"^##\s+(?P<heading>.+?)\s*\{#(?P<sid>[a-z0-9_]+)\}\s*$")
REVIEW_STATUSES = {"DRAFT_UNREVIEWED", "REVIEWED"}


class KBError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    section_id: str
    heading: str
    text: str


@dataclass(frozen=True)
class KBDoc:
    doc_id: str
    version: int
    title: str
    scopes: tuple[str, ...]
    review_status: str
    sections: tuple[Section, ...]
    body_hash: str

    def chunk_id(self, section_id: str) -> str:
        return f"{self.doc_id}#{section_id}@v{self.version}"


def parse_doc(path: Path) -> KBDoc:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m:
        raise KBError(f"{path.name}: missing front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    for field in ("id", "version", "title", "scopes", "review_status"):
        if field not in meta:
            raise KBError(f"{path.name}: front matter missing '{field}'")
    if meta["id"] != path.stem:
        raise KBError(f"{path.name}: id '{meta['id']}' must equal the file name")
    if not isinstance(meta["version"], int) or meta["version"] < 1:
        raise KBError(f"{path.name}: version must be a positive integer")
    if meta["review_status"] not in REVIEW_STATUSES:
        raise KBError(f"{path.name}: unknown review_status {meta['review_status']!r}")

    sections: list[Section] = []
    current: tuple[str, str] | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is None:
            return
        text = "\n".join(buf).strip()
        if not text:
            raise KBError(f"{path.name}: section '{current[0]}' is empty")
        sections.append(Section(current[0], current[1], text))

    for line in m.group(2).splitlines():
        if line.startswith("## "):
            sm = SECTION_RE.match(line)
            if not sm:
                raise KBError(f"{path.name}: section heading without stable id: {line!r}")
            flush()
            current, buf = (sm["sid"], sm["heading"]), []
        elif current is not None:
            buf.append(line)
    flush()
    if not sections:
        raise KBError(f"{path.name}: no sections")
    ids = [s.section_id for s in sections]
    if len(ids) != len(set(ids)):
        raise KBError(f"{path.name}: duplicate section ids")
    return KBDoc(
        doc_id=meta["id"],
        version=meta["version"],
        title=meta["title"],
        scopes=tuple(meta["scopes"]),
        review_status=meta["review_status"],
        sections=tuple(sections),
        body_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def load_kb_dir(kb_dir: Path) -> list[KBDoc]:
    return [parse_doc(p) for p in sorted(kb_dir.glob("*.md"))]


def index_docs(conn: sqlite3.Connection, docs: list[KBDoc]) -> tuple[int, int]:
    """Index only the current version of each doc (old versions are never searchable)."""
    n_chunks = 0
    for d in docs:
        conn.execute(
            "INSERT OR REPLACE INTO kb_docs(doc_id, version, title, review_status, scopes, body_hash) VALUES (?,?,?,?,?,?)",
            (d.doc_id, d.version, d.title, d.review_status, ",".join(d.scopes), d.body_hash),
        )
        old = [r[0] for r in conn.execute("SELECT chunk_id FROM kb_chunks WHERE doc_id = ?", (d.doc_id,))]
        for cid in old:
            conn.execute("DELETE FROM kb_chunks_fts WHERE chunk_id = ?", (cid,))
        conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (d.doc_id,))
        for s in d.sections:
            cid = d.chunk_id(s.section_id)
            conn.execute(
                "INSERT INTO kb_chunks(chunk_id, doc_id, version, section_id, heading, text) VALUES (?,?,?,?,?,?)",
                (cid, d.doc_id, d.version, s.section_id, s.heading, s.text),
            )
            conn.execute("INSERT INTO kb_chunks_fts(chunk_id, heading, text) VALUES (?,?,?)", (cid, s.heading, s.text))
            n_chunks += 1
    return len(docs), n_chunks
