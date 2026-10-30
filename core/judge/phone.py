"""Judge's phone client tokens (#130). A QR token binds a phone to ONE persona and opens exactly ONE session.

State lives in the ledger (HUMAN_ACTION, actor JUDGE): PHONE_TOKEN_ISSUE {persona_id, token_hash, expires_at} and
PHONE_TOKEN_REDEEM {token_hash, session_id}. Only sha256(token) is stored. A token is derived, not random:
``jt_`` + sha256(secret | persona | issue ordinal)[:32]. The secret is injected (config / tests); there is no uuid, no secrets module
and no wall clock. Expiry is SimClock time. The persona comes from the issue entry, never from the caller: a caller-supplied persona
that differs from the bound one is refused and the token is NOT consumed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta

from core.audit.ledger import Ledger
from core.engine.clock import SimClock, iso, parse_iso
from core.judge import session as sess
from core.judge.personas import get_persona

OP_ISSUE, OP_REDEEM = "PHONE_TOKEN_ISSUE", "PHONE_TOKEN_REDEEM"
DEFAULT_TTL_S = 900
TOKEN_PREFIX = "jt_"


class TokenError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ops(conn: sqlite3.Connection, op: str, token_hash: str | None = None):
    sql = "SELECT seq, payload_json FROM ledger WHERE kind='HUMAN_ACTION' AND actor=? AND payload_json LIKE ?"
    pat = f'%"op":"{op}"%' + (f'%"token_hash":"{token_hash}"%' if token_hash else "")
    return [(r[0], json.loads(r[1])) for r in conn.execute(sql + " ORDER BY seq", (sess.ACTOR, pat))]


def issue_token(conn: sqlite3.Connection, ledger: Ledger, clock: SimClock, *, secret: str, persona_id: str, ttl_s: int = DEFAULT_TTL_S) -> dict:
    if not secret:
        raise ValueError("phone token secret is not configured")
    card = get_persona(conn, persona_id)                       # KeyError for an unknown persona
    ordinal = len(_ops(conn, OP_ISSUE)) + 1
    token = TOKEN_PREFIX + hashlib.sha256(f"{secret}|{card.persona_id}|{ordinal}".encode("utf-8")).hexdigest()[:32]
    expires = iso(clock.now() + timedelta(seconds=ttl_s))
    e = ledger.append(kind="HUMAN_ACTION", actor=sess.ACTOR, role="judge",
                      payload={"op": OP_ISSUE, "persona_id": card.persona_id, "token_hash": _hash(token), "expires_at": expires, "label": "SIMULATED"})
    return {"token": token, "persona_id": card.persona_id, "expires_at": expires, "issue_seq": e.seq}


def redeem_token(conn: sqlite3.Connection, ledger: Ledger, clock: SimClock, *, token: str, persona_id: str | None = None) -> sess.JudgeSession:
    """Opens the one session this token allows. Raises TokenError(UNKNOWN_TOKEN | TOKEN_USED | TOKEN_EXPIRED | PERSONA_MISMATCH).
    The check and the writes happen in one write transaction, so two simultaneous redeems cannot both succeed."""
    h = _hash(token or "")
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        issues = _ops(conn, OP_ISSUE, h)
        if not issues or issues[0][1].get("token_hash") != h:
            raise TokenError("UNKNOWN_TOKEN")
        bound = issues[0][1]
        if persona_id is not None and persona_id != bound["persona_id"]:
            raise TokenError("PERSONA_MISMATCH")
        if _ops(conn, OP_REDEEM, h):
            raise TokenError("TOKEN_USED")
        if clock.now() >= parse_iso(bound["expires_at"]):
            raise TokenError("TOKEN_EXPIRED")
        s = sess.start_session(conn, ledger, bound["persona_id"])
        ledger.append(kind="HUMAN_ACTION", actor=sess.ACTOR, role="judge", payload={"op": OP_REDEEM, "token_hash": h, "session_id": s.session_id})
        if own:
            conn.execute("COMMIT")
        return s
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
