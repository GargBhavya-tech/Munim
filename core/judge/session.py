"""Judge sessions (#129). A session is nothing but ledger entries, so it cannot disagree with the ledger.

HUMAN_ACTION entries (case_id NULL, actor ``JUDGE``) with ``op`` in JUDGE_SESSION_START / JUDGE_ATTEMPT / JUDGE_FEEDBACK. The session id is
``JS-<seq of its start entry>`` (deterministic). A session is bound to one persona (#128) and therefore one merchant: messages are
always addressed to that merchant. There is no second code path: messages go through the production merchant-message flow.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from core.audit.ledger import Ledger
from core.judge.personas import get_persona

OP_START, OP_ATTEMPT, OP_FEEDBACK = "JUDGE_SESSION_START", "JUDGE_ATTEMPT", "JUDGE_FEEDBACK"
ACTOR = "JUDGE"


class UnknownSession(KeyError):
    pass


@dataclass(frozen=True)
class JudgeSession:
    session_id: str
    persona_id: str
    merchant_id: str
    start_seq: int

    def as_json(self) -> dict:
        return {"session_id": self.session_id, "persona_id": self.persona_id, "merchant_id": self.merchant_id, "start_seq": self.start_seq}


def start_session(conn: sqlite3.Connection, ledger: Ledger, persona_id: str) -> JudgeSession:
    card = get_persona(conn, persona_id)            # KeyError for an unknown persona
    e = ledger.append(kind="HUMAN_ACTION", actor=ACTOR, role="judge",
                      payload={"op": OP_START, "persona_id": card.persona_id, "merchant_id": card.merchant_id, "label": "SIMULATED"})
    return JudgeSession(f"JS-{e.seq}", card.persona_id, card.merchant_id, e.seq)


def load_session(conn: sqlite3.Connection, session_id: str) -> JudgeSession:
    try:
        seq = int(session_id.removeprefix("JS-")) if session_id.startswith("JS-") else -1
    except ValueError:
        seq = -1
    r = conn.execute("SELECT payload_json, kind, actor FROM ledger WHERE seq = ?", (seq,)).fetchone() if seq > 0 else None
    if r is None or r[1] != "HUMAN_ACTION" or r[2] != ACTOR:
        raise UnknownSession(session_id)
    p = json.loads(r[0])
    if p.get("op") != OP_START:
        raise UnknownSession(session_id)
    return JudgeSession(session_id, p["persona_id"], p["merchant_id"], seq)


def record_attempt(ledger: Ledger, s: JudgeSession, *, chars: int) -> int:
    """One merchant-side attempt. Written BEFORE the engine is called. The text itself is not stored here (the redacted copy is in the event)."""
    return ledger.append(kind="HUMAN_ACTION", actor=ACTOR, role="judge",
                         payload={"op": OP_ATTEMPT, "session_id": s.session_id, "chars": chars}).seq


def record_feedback(ledger: Ledger, s: JudgeSession, *, verdict: str, ref_seq: int | None) -> int:
    """The 'that was fine' button. A different op from an attempt, so it never changes the attempt count."""
    return ledger.append(kind="HUMAN_ACTION", actor=ACTOR, role="judge",
                         payload={"op": OP_FEEDBACK, "session_id": s.session_id, "verdict": verdict, "ref_seq": ref_seq}).seq
