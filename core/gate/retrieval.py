"""Hybrid retrieval over the approved knowledge base: BM25 (FTS5) + dense embeddings, fused by RRF, then a cross-encoder rerank.

* Only CURRENT doc versions are searchable: every query joins ``kb_chunks`` to ``kb_docs`` on ``(doc_id, version)``, so a stale
  chunk left in the FTS table or in ``kb_vectors`` can never be returned.
* Final order: with a reranker, RRF over three ranked lists (bm25, dense, cross-encoder) of the top ``candidates``; without one,
  RRF over bm25 and dense. ``scores["rrf"]`` is always the two-list first-stage RRF; ``scores["final"]`` is the ordering score.
* ``scope`` is the playbook's ``kb_scope`` (a list of doc ``scopes``). Docs outside it are excluded BEFORE ranking.
* Every ``Hit`` carries provenance: doc_id, version, section, method and the scores that produced its position.

``best_score`` scale (the novelty signal for escalation): a float in [0, 1], higher = the KB knows the answer better.
With a reranker it is the best cross-encoder relevance (sigmoid probability) among the top 5 hits. Without one it is the best dense
cosine similarity clipped to [0, 1]. Novelty = 1 - best_score. 0.0 means nothing in scope matched at all. The two scales are NOT
interchangeable, so a threshold must be chosen for the configuration actually deployed.
"""
from __future__ import annotations

import math
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence

import numpy as np

from core.gate.embedder import Embedder

RRF_K = 60
DEFAULT_RERANKER = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


def rrf_score(ranks: Iterable[int | None], k: int = RRF_K) -> float:
    """Reciprocal Rank Fusion: sum of 1/(k+rank) over the lists the item appears in (rank is 1-based; None = absent)."""
    return sum(1.0 / (k + r) for r in ranks if r is not None)


def fts_query(text: str) -> str:
    """Turn arbitrary text into a safe FTS5 MATCH expression (quoted terms joined by OR); '' when nothing searchable.

    Letters, digits and combining marks (Indic matras) are kept; everything else (quotes, apostrophes, operators,
    punctuation, NEAR/AND/OR/NOT) is neutralised because each term is double-quoted.
    """
    cleaned = "".join(c if (c.isalnum() or unicodedata.category(c).startswith("M")) else " " for c in text)
    terms = [t for t in cleaned.split() if t]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


# Small domain glossary: Hindi / Hinglish / Kannada stems -> English KB terms. The KB is English, so the keyword channel (and the
# dense and rerank queries) get these English terms appended. Matching is by lower-cased substring so Kannada inflections
# (ದಾಖಲೆಗಳು = ದಾಖಲೆ + ಗಳು) hit. Deterministic, reviewable, and NOT a translation: the original query is always kept.
# Native-speaker review expected; entries are limited to words that are certain.
GLOSSARY: dict[str, str] = {
    # Hindi / Hinglish
    "दस्तावेज": "documents", "dastavez": "documents", "कागज": "documents", "kagaz": "documents",
    "ब्याज": "interest rate", "byaj": "interest rate", "byaaj": "interest rate", "सूद": "interest rate",
    "लोन": "loan", "कर्ज": "loan", "karz": "loan",
    "पैसा": "money payment settlement", "पैसे": "money payment settlement", "paisa": "money payment settlement",
    "paise": "money payment settlement",
    "बैंक": "bank", "नहीं आया": "not received", "नहीं पहुंचा": "not received", "nahi aaya": "not received",
    "nahi pahuncha": "not received", "nahi aya": "not received",
    "band hai": "switched off offline not working", "बंद हो गया": "switched off offline not working",
    "बंद है": "switched off offline not working", "kaam nahi": "not working", "काम नहीं": "not working",
    "खाता बंद": "close account closure", "khata band": "close account closure", "account band": "close account closure",
    "साउंडबॉक्स": "soundbox", "रिफंड": "refund", "रद्द": "cancel", "radd": "cancel", "kitna": "how much",
    # Kannada
    "ದಾಖಲೆ": "documents", "ಬಡ್ಡಿ": "interest rate", "ಸಾಲ": "loan", "ಹಣ": "money payment settlement",
    "ಬ್ಯಾಂಕ್": "bank", "ಬಂದಿಲ್ಲ": "not received", "ಸೌಂಡ್": "soundbox", "ಕೆಲಸ ಮಾಡುತ್ತಿಲ್ಲ": "not working offline",
    "ಮುಚ್ಚ": "close account closure", "ರೀಫಂಡ್": "refund", "ಕೆವೈಸಿ": "KYC",
}


def expand_query(text: str, glossary: dict[str, str] | None = None) -> str:
    """Append English glosses for any glossary stems found in ``text`` (original text kept first)."""
    g = GLOSSARY if glossary is None else glossary
    low = text.lower()
    extra: list[str] = []
    for stem, gloss in g.items():
        if stem in low and gloss not in extra:
            extra.append(gloss)
    return text if not extra else f"{text} {' '.join(extra)}"


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    version: int
    section_id: str
    heading: str
    text: str
    parent_text: str
    scores: dict = field(default_factory=dict)  # bm25_rank, dense_rank, rrf, rerank (+ dense_cos)
    method: str = "hybrid"

    @property
    def citation(self) -> str:
        return self.chunk_id


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    """Multilingual cross-encoder (loaded lazily). Scores are relevance probabilities in [0, 1]."""

    def __init__(self, model_name: str = DEFAULT_RERANKER, device: str = "cpu"):
        self.name = model_name
        self.device = device
        self._model = None

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.name, device=self.device)
        import torch
        raw = self._model.predict([(query, p) for p in passages], activation_fn=torch.nn.Identity(), show_progress_bar=False)
        return [float(1.0 / (1.0 + math.exp(-float(x)))) for x in raw]  # explicit sigmoid: bounded [0, 1] whatever the model's head


_DOC_JOIN = "JOIN kb_docs d ON d.doc_id = c.doc_id AND d.version = c.version"
_CURRENT = f"kb_chunks c {_DOC_JOIN}"


def _allowed_scope_sql(scope: Sequence[str] | None) -> tuple[str, list]:
    """SQL fragment + params restricting to docs having at least one scope in ``scope`` (comma list in kb_docs.scopes)."""
    if scope is None:
        return "", []
    if not list(scope):
        return " AND 0", []
    clauses = " OR ".join("(',' || REPLACE(COALESCE(d.scopes,''),' ','') || ',') LIKE ?" for _ in scope)
    return f" AND ({clauses})", [f"%,{s},%" for s in scope]


def index_vectors(conn: sqlite3.Connection, embedder: Embedder) -> int:
    """(Re)build kb_vectors for CURRENT chunks only with this embedder. Returns the number of vectors stored.

    Vectors of non-current chunks and of other models are deleted, so a model change or a doc-version bump cannot leave
    stale rows behind.
    """
    rows = conn.execute(f"SELECT c.chunk_id, c.heading, c.text FROM {_CURRENT} ORDER BY c.chunk_id").fetchall()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM kb_vectors")
        if rows:
            vecs = embedder.encode([f"{r['heading']}. {r['text']}" for r in rows], kind="passage")
            conn.executemany(
                "INSERT INTO kb_vectors(chunk_id, model, dim, vector) VALUES (?,?,?,?)",
                [(r["chunk_id"], embedder.name, int(vecs.shape[1]), vecs[i].astype(np.float32).tobytes()) for i, r in enumerate(rows)],
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


class Retriever:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder, reranker: Reranker | None = None,
                 candidates: int = 10, pool: int = 20, glossary: dict[str, str] | None = None):
        self.conn = conn
        self.embedder = embedder
        self.reranker = reranker
        self.candidates = candidates  # how many RRF leaders go to the reranker
        self.pool = pool              # depth of each first-stage list
        self.glossary = GLOSSARY if glossary is None else glossary  # pass {} to disable query expansion

    # ---- first stage -------------------------------------------------------------------------------------------
    def _bm25(self, query: str, scope) -> list[str]:
        q = fts_query(query)
        if not q:
            return []
        frag, params = _allowed_scope_sql(scope)
        sql = (f"SELECT c.chunk_id FROM kb_chunks_fts f JOIN kb_chunks c ON c.chunk_id = f.chunk_id {_DOC_JOIN} "
               f" WHERE kb_chunks_fts MATCH ?{frag} "
               f"ORDER BY bm25(kb_chunks_fts, 0.0, 2.0, 1.0), c.chunk_id LIMIT ?")
        try:
            return [r[0] for r in self.conn.execute(sql, [q, *params, self.pool])]
        except sqlite3.OperationalError:  # defence in depth: a query must never raise
            return []

    def _dense(self, query: str, scope) -> tuple[list[str], dict[str, float]]:
        frag, params = _allowed_scope_sql(scope)
        rows = self.conn.execute(
            f"SELECT v.chunk_id, v.vector, v.dim FROM kb_vectors v JOIN kb_chunks c ON c.chunk_id = v.chunk_id {_DOC_JOIN}  "
            f"WHERE v.model = ?{frag} ORDER BY v.chunk_id", [self.embedder.name, *params]).fetchall()
        if not rows:
            return [], {}
        mat = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
        qv = self.embedder.encode([query], kind="query")[0]
        sims = mat @ qv
        order = np.argsort(-sims, kind="stable")[: self.pool]
        ids = [rows[i]["chunk_id"] for i in order]
        return ids, {rows[i]["chunk_id"]: float(sims[i]) for i in order}

    def _chunk(self, chunk_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT c.chunk_id, c.doc_id, c.version, c.section_id, c.heading, c.text FROM kb_chunks c WHERE c.chunk_id = ?",
                                 (chunk_id,)).fetchone()

    def _parent_text(self, doc_id: str, version: int) -> str:
        rows = self.conn.execute("SELECT heading, text FROM kb_chunks WHERE doc_id = ? AND version = ? ORDER BY rowid",
                                 (doc_id, version)).fetchall()
        return "\n\n".join(f"## {r['heading']}\n{r['text']}" for r in rows)

    # ---- public ------------------------------------------------------------------------------------------------
    def search(self, query: str, scope: Sequence[str] | None = None, k: int = 5) -> list[Hit]:
        query = expand_query(query, self.glossary)
        bm_ids = self._bm25(query, scope)
        dn_ids, cos = self._dense(query, scope)
        bm_rank = {c: i + 1 for i, c in enumerate(bm_ids)}
        dn_rank = {c: i + 1 for i, c in enumerate(dn_ids)}
        fused = {c: rrf_score([bm_rank.get(c), dn_rank.get(c)]) for c in set(bm_rank) | set(dn_rank)}
        order = sorted(fused, key=lambda c: (-fused[c], c))[: max(self.candidates, k)]

        rerank: dict[str, float] = {}
        final: dict[str, float] = {}
        if self.reranker is not None and order:
            texts = []
            for c in order:
                ch = self._chunk(c)
                texts.append(f"{ch['heading']}. {ch['text']}")
            rerank = dict(zip(order, self.reranker.score(query, texts)))
            # The cross-encoder is a THIRD voter in the fusion, not an override: measured on the labelled set, letting a small
            # multilingual reranker overrule BM25+dense pushed correct sections out of the top 3 for Hinglish / Kannada queries.
            rr_rank = {c: i + 1 for i, c in enumerate(sorted(order, key=lambda c: (-rerank[c], c)))}
            final = {c: rrf_score([bm_rank.get(c), dn_rank.get(c), rr_rank[c]]) for c in order}
            order = sorted(order, key=lambda c: (-final[c], -rerank[c], c))
        order = order[:k]

        hits = []
        for pos, c in enumerate(order):
            ch = self._chunk(c)
            in_bm, in_dn = c in bm_rank, c in dn_rank
            method = "hybrid" if in_bm and in_dn else ("bm25" if in_bm else "dense")
            hits.append(Hit(
                chunk_id=c, doc_id=ch["doc_id"], version=ch["version"], section_id=ch["section_id"], heading=ch["heading"],
                text=ch["text"], parent_text=self._parent_text(ch["doc_id"], ch["version"]) if pos < 5 else "",
                scores={"bm25_rank": bm_rank.get(c), "dense_rank": dn_rank.get(c), "rrf": fused[c],
                        "rerank": rerank.get(c), "dense_cos": cos.get(c), "final": final.get(c, fused[c])},
                method=method + ("+rerank" if c in rerank else ""),
            ))
        return hits

    def best_score(self, query: str, scope: Sequence[str] | None = None) -> float:
        """Novelty helper (scale in the module docstring): [0, 1], higher = better covered by the KB; novelty = 1 - best_score.

        With a reranker: the highest cross-encoder probability among the top 5 hits. Measured with mmarco-mMiniLM on the 24 labelled
        queries vs 10 off-topic ones: in-KB median 0.58 (min 0.01), off-topic max 0.05 - so ~0.05-0.1 is a sensible "unknown" cut.
        Without a reranker: the best dense cosine clipped to [0, 1]; with e5-small this is compressed (in-KB min 0.78, off-topic
        max 0.82) and separates poorly, so prefer the reranked value for the novelty signal.
        """
        hits = self.search(query, scope, k=5)
        if not hits:
            return 0.0
        rr = [h.scores["rerank"] for h in hits if h.scores.get("rerank") is not None]
        v = max(rr) if rr else max((h.scores.get("dense_cos") or 0.0) for h in hits)
        return float(min(1.0, max(0.0, v)))
