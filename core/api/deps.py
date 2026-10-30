"""Shared helpers for the dashboard API: short-lived read connections and row -> JSON shapes.

Every request opens its own connection (the ledger is written concurrently by other connections; WAL keeps readers
and the writer out of each other's way) and closes it. Read connections are query_only: the API's SQL is observability.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from fastapi import Request

from core.engine.clock import parse_iso


def open_ro(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_db(request: Request):
    """FastAPI dependency: one short connection per request."""
    conn = open_ro(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_db(request_or_app):
    """A short-lived writable connection (control endpoints and handoff handlers only)."""
    from core.memory.store import connect
    app = getattr(request_or_app, "app", request_or_app)
    conn = connect(app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


CASE_FIELDS = ("case_id", "merchant_id", "lead_id", "playbook_id", "state", "lane", "language", "step", "touches", "opened_at", "closed_at", "resume_note")


def case_json(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in CASE_FIELDS}


def ledger_json(r: sqlite3.Row) -> dict:
    return {"seq": r["seq"], "case_id": r["case_id"], "ts_sim": r["ts_sim"], "actor": r["actor"], "role": r["role"], "kind": r["kind"],
            "payload": json.loads(r["payload_json"]), "policy_hash": r["policy_hash"], "model_id": r["model_id"], "hash": r["hash"]}


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def future(until: str | None, clock) -> bool:
    return bool(until) and clock.now() <= parse_iso(until)


def sent_replies(conn: sqlite3.Connection, merchant_id: str, after: int) -> list[dict]:
    """The MESSAGE_SENT entries for this merchant after ledger seq ``after``: only text the grounding gate let through, with the seq that
    POST /api/voice/speak/{seq} takes."""
    import json
    rows = conn.execute("SELECT l.seq, l.case_id, l.payload_json FROM ledger l JOIN cases c ON c.case_id = l.case_id "
                        "WHERE l.seq > ? AND l.kind = 'MESSAGE_SENT' AND c.merchant_id = ? ORDER BY l.seq", (after, merchant_id)).fetchall()
    return [{"seq": r["seq"], "case_id": r["case_id"], **{k: json.loads(r["payload_json"]).get(k) for k in ("language", "text")}} for r in rows]
