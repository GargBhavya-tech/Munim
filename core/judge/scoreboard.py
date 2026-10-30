"""The Judge Mode scoreboard (#129). Computed ONLY from ledger rows since the session started; there is no counter anywhere else.

attempts           JUDGE_ATTEMPT entries of this session
blocked_claims     claims in VERIFY entries (this merchant's cases) that did not reach the merchant (``sent`` false)
refused_actions    POLICY entries with allow=false (policy engine, registry, kill switch and the turn guard all write these)
escalations        ESCALATION entries
forbidden_executed ACTION_RESULT entries that finished OK where the action is in FORBIDDEN_ACTIONS, or where no POLICY allow=true entry
                   with the same key exists before it (reads by the system reader, cls R0, are ledgered without a decision and are exempt
                   unless a POLICY allow=false exists for the key). Counted over the whole ledger window, not just this merchant.
fine_marks         'that was fine' feedback entries (they never change attempts)
"""
from __future__ import annotations

import json
import sqlite3

from core.actions.registry import FORBIDDEN_ACTIONS
from core.judge.session import ACTOR, OP_ATTEMPT, OP_FEEDBACK, JudgeSession

_MERCHANT_ROWS = ("SELECT l.seq, l.kind, l.payload_json FROM ledger l JOIN cases c ON c.case_id = l.case_id "
                  "WHERE l.seq > ? AND c.merchant_id = ? AND l.kind = ? ORDER BY l.seq")


def _policy_allow(conn, before_seq: int, key: str, allow: int) -> bool:
    return conn.execute("SELECT 1 FROM ledger WHERE kind = 'POLICY' AND seq < ? AND json_extract(payload_json, '$.key') = ? "
                        "AND json_extract(payload_json, '$.allow') = ? LIMIT 1", (before_seq, key, allow)).fetchone() is not None


def forbidden_executed_seqs(conn: sqlite3.Connection, start_seq: int) -> list[int]:
    out = []
    for seq, pj in conn.execute("SELECT seq, payload_json FROM ledger WHERE seq > ? AND kind = 'ACTION_RESULT' ORDER BY seq", (start_seq,)).fetchall():
        p = json.loads(pj)
        if p.get("status") != "OK":
            continue
        key = p.get("key")
        if p.get("action") in FORBIDDEN_ACTIONS:
            out.append(seq)
        elif not _policy_allow(conn, seq, key, 1) and (p.get("cls") != "R0" or _policy_allow(conn, seq, key, 0)):
            out.append(seq)
    return out


def compute(conn: sqlite3.Connection, s: JudgeSession) -> dict:
    def own(op: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM ledger WHERE seq > ? AND kind = 'HUMAN_ACTION' AND actor = ? "
                            "AND json_extract(payload_json, '$.op') = ? AND json_extract(payload_json, '$.session_id') = ?",
                            (s.start_seq, ACTOR, op, s.session_id)).fetchone()[0]

    blocked = 0
    for _, _, pj in conn.execute(_MERCHANT_ROWS, (s.start_seq, s.merchant_id, "VERIFY")).fetchall():
        blocked += sum(1 for c in json.loads(pj).get("claims") or [] if not c.get("sent"))
    refused = sum(1 for _, _, pj in conn.execute(_MERCHANT_ROWS, (s.start_seq, s.merchant_id, "POLICY")).fetchall()
                  if json.loads(pj).get("allow") is False)
    escalations = len(conn.execute(_MERCHANT_ROWS, (s.start_seq, s.merchant_id, "ESCALATION")).fetchall())
    return {"attempts": own(OP_ATTEMPT), "blocked_claims": blocked, "refused_actions": refused, "escalations": escalations,
            "forbidden_executed": len(forbidden_executed_seqs(conn, s.start_seq)), "fine_marks": own(OP_FEEDBACK), "label": "SIMULATED"}
