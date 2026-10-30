"""Outbox for R3 sends (#26).

The outbox row is written in the SAME transaction as the actions row, the idempotency reservation and the
ACTION_INTENT/POLICY ledger entries. A sender delivers AFTER that commit and marks the row sent together
with the ACTION_RESULT. Nothing is ever sent inside a transaction that can still roll back. A crash between
delivery and the mark is safe: the row is re-delivered with the SAME key and the mock replays its response.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from core.engine.clock import SimClock
from core.actions.types import Principal

DDL = """
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL,
  system TEXT, method TEXT, path TEXT, body_json TEXT, principal_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, sent_at TEXT
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)


class Outbox:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock
        ensure_schema(conn)
        self._sender: Callable[[str, Principal], object] | None = None

    def bind(self, sender: Callable[[str, Principal], object]) -> None:
        """The executor registers how a row is delivered (call the mock, record the result)."""
        self._sender = sender

    # -- writes (caller holds the transaction) ------------------------------------------------------------
    def write(self, *, action_id: str, key: str, system: str | None, method: str | None, path: str | None, body: dict | None,
              principal: Principal) -> None:
        self.conn.execute(
            "INSERT INTO outbox(action_id, idempotency_key, system, method, path, body_json, principal_json, status, created_at)"
            " VALUES (?,?,?,?,?,?,?, 'PENDING', ?)",
            (action_id, key, system, method, path, json.dumps(body, sort_keys=True) if body is not None else None,
             json.dumps({"kind": principal.kind, "id": principal.id, "role": principal.role}), self.clock.now_iso()))

    def mark_sent(self, action_id: str, status: str = "SENT") -> None:
        self.conn.execute("UPDATE outbox SET status = ?, sent_at = ? WHERE action_id = ?", (status, self.clock.now_iso(), action_id))

    def bump(self, action_id: str) -> None:
        self.conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE action_id = ?", (action_id,))

    # -- reads --------------------------------------------------------------------------------------------
    def get(self, action_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM outbox WHERE action_id = ?", (action_id,)).fetchone()
        return dict(r) if r else None

    def pending(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM outbox WHERE status = 'PENDING' ORDER BY outbox_id")]

    def kill_switch_on(self) -> bool:
        r = self.conn.execute("SELECT value FROM meta WHERE key = 'kill_switch'").fetchone()
        return bool(r) and r["value"] == "on"

    def deliver_pending(self) -> list:
        """Deliver every unsent row once, oldest first. With the kill switch on, nothing is sent (queued rows stay queued)."""
        if self._sender is None:
            raise RuntimeError("outbox has no sender bound; construct it through the Executor")
        if self.kill_switch_on():
            return []
        out = []
        for row in self.pending():
            pj = json.loads(row["principal_json"])
            out.append(self._sender(row["action_id"], Principal(pj["kind"], pj["id"], pj["role"])))
        return out
