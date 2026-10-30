"""Knowledge layer: the answering path for C8 and the gaps table (Bible section 17, ticket #77).

Rules this module enforces in code, not in a prompt:
  * An answer is built ONLY from approved KB hits that sit inside the playbook's kb_scope.
  * A hit that comes from a past ticket is a DIAGNOSIS HINT. It is kept apart (``Answer.hints``) and can never reach
    ``Answer.text`` or ``Answer.citations``: old tickets can hold wrong advice or another merchant's details.
  * A question with no usable KB hit is NOVEL: no text, ``escalate`` is true, the question travels with the handoff and a
    gap row is written. Unsupported (X0) requests are logged the same way, with the text REDACTED.
  * Gaps are grouped weekly for a human who writes the missing article. A draft article is stored with ``enabled = 0`` and a
    CHECK constraint keeps it off until a named person approves it: nothing is ever auto-enabled.

The retriever is injected (any object with ``search(query, scope, k)``); this module imports no retrieval code.
Time comes from the injected SimClock, ids are deterministic.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from core.engine.clock import SimClock, parse_iso
from core.memory.store import write_txn

NO_ANSWER_REASON = "NO_KB_ANSWER"
GAP_KINDS = ("NO_ANSWER", "UNSUPPORTED")
DEFAULT_MIN_SCORE = 0.2
DEFAULT_K = 5
MAX_PASSAGES = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_gaps (
  gap_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('NO_ANSWER', 'UNSUPPORTED')),
  merchant_id TEXT,
  case_id TEXT,
  question_redacted TEXT NOT NULL,
  language TEXT,
  scope_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'DRAFTED', 'PUBLISHED', 'DISMISSED')),
  draft_text TEXT,
  drafted_at TEXT,
  approved_by TEXT,
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  CHECK (enabled = 0 OR (approved_by IS NOT NULL AND approved_by <> ''))
);
CREATE INDEX IF NOT EXISTS idx_gaps_created ON knowledge_gaps(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gaps_case_dedupe ON knowledge_gaps(case_id, kind, question_redacted) WHERE case_id IS NOT NULL;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ------------------------------------------------------------------------------------------------ retrieval contract
class Retriever(Protocol):
    """Anything that can search approved knowledge. A hit has chunk_id, doc_id, section_id, text and score
    (an optional ``source`` of 'ticket' marks a past-ticket hit; a chunk_id or doc_id starting 'ticket' does too)."""

    def search(self, query: str, scope: Sequence[str] | None, k: int) -> list[Any]: ...


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    doc_id: str
    section_id: str
    text: str
    score: float


def _score(hit: Any) -> float:
    s = getattr(hit, "score", None)
    if s is None:
        scores = getattr(hit, "scores", None) or {}
        numeric = {k: v for k, v in scores.items() if isinstance(v, (int, float))}      # a stage that did not run reports None
        s = numeric.get("rerank", numeric.get("rrf", max(numeric.values(), default=0.0)))
    return float(s)


def is_ticket_hit(hit: Any) -> bool:
    """True for anything sourced from a past ticket. Checked on three independent markers so one missing label cannot let it through."""
    if str(getattr(hit, "source", "kb") or "kb").lower().startswith("ticket"):
        return True
    return any(str(getattr(hit, f, "") or "").lower().startswith("ticket") for f in ("chunk_id", "doc_id"))


def _passage(hit: Any) -> Passage:
    return Passage(str(hit.chunk_id), str(hit.doc_id), str(hit.section_id), str(hit.text), _score(hit))


@dataclass(frozen=True)
class Answer:
    status: str                                   # ANSWERED | NOVEL
    question: str                                 # verbatim, for the handoff (never logged unredacted)
    scope: tuple[str, ...]
    text: str | None = None                       # composed ONLY from `passages`
    passages: tuple[Passage, ...] = ()
    hints: tuple[Passage, ...] = ()               # past-ticket hits: for diagnosis, never quoted
    gap_id: str | None = None

    @property
    def citations(self) -> list[str]:
        return [p.chunk_id for p in self.passages]

    @property
    def escalate(self) -> bool:
        return self.status == "NOVEL"

    @property
    def reasons(self) -> list[str]:
        return [NO_ANSWER_REASON] if self.escalate else []

    @property
    def handoff(self) -> dict | None:
        """What the human receives when the question could not be answered: the question itself, the reason, the scope searched."""
        if not self.escalate:
            return None
        return {"reason": NO_ANSWER_REASON, "question": self.question, "scope": list(self.scope), "gap_id": self.gap_id}


class KnowledgeLayer:
    def __init__(self, retriever: Retriever, *, doc_scopes: Mapping[str, Sequence[str]] | None = None, conn: sqlite3.Connection | None = None,
                 clock: SimClock | None = None, min_score: float = DEFAULT_MIN_SCORE, k: int = DEFAULT_K):
        """``doc_scopes`` maps doc_id -> the scopes that document belongs to. With it, a hit whose document is outside the asked scope is
        dropped even if the retriever returned it. Without it a hit is accepted when its doc_id is itself one of the scopes."""
        self.retriever, self.doc_scopes, self.conn, self.clock = retriever, doc_scopes, conn, clock
        self.min_score, self.k = min_score, k
        if conn is not None:
            ensure_schema(conn)

    # -- scope ------------------------------------------------------------------------------------------------
    def _in_scope(self, hit: Any, scope: Sequence[str]) -> bool:
        doc = str(hit.doc_id)
        if self.doc_scopes is not None:
            return bool(set(self.doc_scopes.get(doc, ())) & set(scope))
        return doc in scope

    # -- answering --------------------------------------------------------------------------------------------
    def search(self, question: str, scope: Sequence[str]) -> tuple[list[Passage], list[Passage]]:
        """(kb passages usable in an answer, past-ticket hints), best first."""
        scope = list(scope)
        kb, hints = [], []
        for h in self.retriever.search(question, scope, self.k) or []:
            if is_ticket_hit(h):
                hints.append(_passage(h))
            elif self._in_scope(h, scope) and _score(h) >= self.min_score and str(h.text).strip():
                kb.append(_passage(h))
        key = lambda p: (-p.score, p.chunk_id)
        return sorted(kb, key=key), sorted(hints, key=key)

    def answer(self, question: str, scope: Sequence[str]) -> Answer:
        kb, hints = self.search(question, scope)
        if not kb:
            return Answer("NOVEL", question, tuple(scope), None, (), tuple(hints))
        top = tuple(kb[:MAX_PASSAGES])
        return Answer("ANSWERED", question, tuple(scope), "\n\n".join(p.text.strip() for p in top), top, tuple(hints))

    def respond(self, question: str, scope: Sequence[str], *, merchant_id: str | None = None, case_id: str | None = None,
                language: str | None = None) -> Answer:
        """answer(), and a NOVEL result is written to the gaps table with the question redacted."""
        ans = self.answer(question, scope)
        if ans.escalate and self.conn is not None and self.clock is not None:
            gid = log_gap(self.conn, self.clock, kind="NO_ANSWER", text=question, merchant_id=merchant_id, case_id=case_id,
                          language=language, scope=scope)
            ans = Answer(ans.status, ans.question, ans.scope, None, (), ans.hints, gid)
        return ans

    def diagnosis_hints(self, question: str, scope: Sequence[str] | None = None) -> list[str]:
        """What fixed similar tickets before, as plain hints for the diagnosis step. Never put these in a merchant message."""
        return [p.text for p in self.search(question, list(scope or []))[1]]


# ------------------------------------------------------------------------------------------------ grounding check
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def ungrounded_numbers(draft: str, answer: Answer) -> list[str]:
    """Numbers in a drafted reply that appear in none of the cited KB passages (a claim the KB does not support)."""
    source = " ".join(p.text for p in answer.passages)
    have = {n.replace(",", "") for n in _NUM.findall(source)}
    return [n for n in _NUM.findall(draft) if n.replace(",", "") not in have]


# ------------------------------------------------------------------------------------------------ redaction
_PATTERNS = [
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"(?i)\b(otp|pin|code|password|cvv)\b([^\d\n]{0,12})(\d{3,8})\b"), r"\1\2[SECRET]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"), "[ID_NUMBER]"),
    (re.compile(r"(?<!\d)\d{9,18}(?!\d)"), "[ACCOUNT_NUMBER]"),
]


def redact(text: str) -> str:
    """Remove what must not sit in a gaps table: phone numbers, account and ID numbers, PAN, e-mail, OTP/PIN values.
    Everything else stays verbatim."""
    out = text or ""
    for rx, sub in _PATTERNS:
        out = rx.sub(sub, out)
    return out.strip()


# ------------------------------------------------------------------------------------------------ gaps
def log_gap(conn: sqlite3.Connection, clock: SimClock, *, kind: str, text: str, merchant_id: str | None = None, case_id: str | None = None,
            language: str | None = None, scope: Sequence[str] = ()) -> str:
    """Write one gap (an unanswered question or an unsupported request). The text is stored REDACTED, verbatim otherwise.
    The same case never logs the same gap twice (an engine retry returns the existing id)."""
    if kind not in GAP_KINDS:
        raise ValueError(f"unknown gap kind {kind!r}")
    ensure_schema(conn)
    red = redact(text)
    with write_txn(conn):
        if case_id is not None:
            row = conn.execute("SELECT gap_id FROM knowledge_gaps WHERE case_id = ? AND kind = ? AND question_redacted = ?", (case_id, kind, red)).fetchone()
            if row:
                return row[0]
        n = conn.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0] + 1
        gid = f"GAP-{n:05d}"
        conn.execute("INSERT INTO knowledge_gaps(gap_id, kind, merchant_id, case_id, question_redacted, language, scope_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (gid, kind, merchant_id, case_id, red, language, json.dumps(list(scope)), clock.now_iso()))
    return gid


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def normalise_question(text: str) -> str:
    """The grouping key: lower-case words only, digits and placeholders dropped, order kept."""
    t = re.sub(r"\[[A-Z_]+\]", " ", text.lower())
    return " ".join(w for w in _WORD.findall(t) if not w.isdigit())


def group_gaps_weekly(conn: sqlite3.Connection) -> list[dict]:
    """OPEN gaps grouped by (ISO week of creation, kind, normalised question), most repeated first.
    Each group: {week, kind, question, count, gap_ids, merchants}. Gaps with a draft are still listed until published."""
    ensure_schema(conn)
    groups: dict[tuple, dict] = {}
    for r in conn.execute("SELECT * FROM knowledge_gaps WHERE status IN ('OPEN', 'DRAFTED') ORDER BY created_at, gap_id"):
        y, w, _ = parse_iso(r["created_at"]).isocalendar()
        key = (f"{y}-W{w:02d}", r["kind"], normalise_question(r["question_redacted"]))
        g = groups.setdefault(key, {"week": key[0], "kind": r["kind"], "question": r["question_redacted"], "count": 0, "gap_ids": [], "merchants": set()})
        g["count"] += 1
        g["gap_ids"].append(r["gap_id"])
        if r["merchant_id"]:
            g["merchants"].add(r["merchant_id"])
    out = [{**g, "merchants": sorted(g["merchants"])} for g in groups.values()]
    return sorted(out, key=lambda g: (g["week"], -g["count"], g["question"]))


def attach_draft(conn: sqlite3.Connection, clock: SimClock, gap_ids: Sequence[str], draft_text: str) -> None:
    """A human's (or a model's) DRAFT article for a group of gaps. Stored with enabled = 0: a draft is never switched on here."""
    ensure_schema(conn)
    with write_txn(conn):
        for gid in gap_ids:
            conn.execute("UPDATE knowledge_gaps SET draft_text = ?, drafted_at = ?, status = 'DRAFTED', enabled = 0 WHERE gap_id = ? AND status IN ('OPEN','DRAFTED')",
                         (draft_text, clock.now_iso(), gid))


def approve_draft(conn: sqlite3.Connection, gap_ids: Sequence[str], approver: str) -> None:
    """Only a named person enables a draft (the CHECK constraint refuses enabled = 1 without one). Indexing the article into the
    KB is a separate, versioned step done by the KB owner."""
    if not approver or not approver.strip():
        raise ValueError("a draft is enabled by a named approver only")
    with write_txn(conn):
        for gid in gap_ids:
            conn.execute("UPDATE knowledge_gaps SET approved_by = ?, enabled = 1, status = 'PUBLISHED' WHERE gap_id = ? AND draft_text IS NOT NULL", (approver.strip(), gid))
