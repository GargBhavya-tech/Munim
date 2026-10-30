"""In-process event backbone (#118) built BESIDE core.engine.events, over the same append-only ``events`` table.

Semantics (the Kafka-shaped contract a Redpanda adapter would keep):
  * ordered: events are delivered in ``seq`` order, so per merchant they arrive in the order they were appended;
  * durable cursors: each consumer has a row in ``backbone_cursors`` (last delivered seq) that survives a restart;
  * at-least-once: the cursor moves only AFTER the handler returned; a handler error leaves the event (and everything
    behind it) undelivered for the next poll, preserving order;
  * deduplicated by event id: ``backbone_delivered(consumer, event_id)`` records successes, so a replay (cursor reset)
    or a re-poll after a crash never invokes the handler a second time for an event it already completed. A crash between
    the handler's side effect and the delivered row is the one at-least-once window; handlers must therefore act through
    the executor, whose deterministic idempotency keys make the repeat a replay, not a second effect.
No wall clock; no new dependency.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from core.engine.events import AppendResult, append_event
from core.memory.store import write_txn

_DDL = (
    "CREATE TABLE IF NOT EXISTS backbone_cursors (consumer TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS backbone_delivered (consumer TEXT NOT NULL, event_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "PRIMARY KEY (consumer, event_id))",
)


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    type: str
    merchant_id: str | None
    case_id: str | None
    payload: dict
    ts_sim: str


Handler = Callable[[Event], None]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _DDL:           # one statement at a time: never executescript
        conn.execute(stmt)


class Backbone:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._handlers: dict[str, Handler] = {}
        self._types: dict[str, frozenset[str] | None] = {}
        ensure_schema(conn)

    # ------------------------------------------------------------------ producing
    def publish(self, *, event_id: str, type: str, payload: dict, ts_sim: str,
                merchant_id: str | None = None, case_id: str | None = None) -> AppendResult:
        return append_event(self.conn, event_id=event_id, type=type, payload=payload, ts_sim=ts_sim,
                            merchant_id=merchant_id, case_id=case_id)

    # ------------------------------------------------------------------ consuming
    def subscribe(self, consumer: str, handler: Handler, *, types: frozenset[str] | set[str] | None = None,
                  from_seq: int = 0) -> None:
        """Register ``handler``; a brand-new consumer starts at ``from_seq`` (an existing cursor is kept)."""
        self._handlers[consumer] = handler
        self._types[consumer] = frozenset(types) if types is not None else None
        with write_txn(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO backbone_cursors(consumer, last_seq) VALUES (?, ?)", (consumer, from_seq))

    def cursor(self, consumer: str) -> int:
        r = self.conn.execute("SELECT last_seq FROM backbone_cursors WHERE consumer = ?", (consumer,)).fetchone()
        if r is None:
            raise KeyError(consumer)
        return r["last_seq"]

    def lag(self, consumer: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events WHERE seq > ?", (self.cursor(consumer),)).fetchone()[0]

    def replay(self, consumer: str, from_seq: int = 0) -> None:
        """Rewind the cursor. Events already completed by this consumer are skipped by the dedupe table."""
        self.cursor(consumer)
        with write_txn(self.conn):
            self.conn.execute("UPDATE backbone_cursors SET last_seq = ? WHERE consumer = ?", (from_seq, consumer))

    def poll(self, consumer: str, limit: int = 100) -> int:
        """Deliver up to ``limit`` events after the cursor. Returns how many handler invocations succeeded.
        Stops at the first handler error (re-raised) so order is kept and the event is retried next poll."""
        handler = self._handlers[consumer]
        types = self._types[consumer]
        rows = self.conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (self.cursor(consumer), limit)).fetchall()
        done = 0
        for r in rows:
            if types is None or r["type"] in types:
                seen = self.conn.execute("SELECT 1 FROM backbone_delivered WHERE consumer = ? AND event_id = ?",
                                         (consumer, r["event_id"])).fetchone()
                if seen is None:
                    handler(Event(r["seq"], r["event_id"], r["type"], r["merchant_id"], r["case_id"],
                                  json.loads(r["payload_json"]), r["ts_sim"]))
                    done += 1
            with write_txn(self.conn):
                self.conn.execute("INSERT OR IGNORE INTO backbone_delivered(consumer, event_id, seq) VALUES (?,?,?)",
                                  (consumer, r["event_id"], r["seq"]))
                self.conn.execute("UPDATE backbone_cursors SET last_seq = ? WHERE consumer = ?", (r["seq"], consumer))
        return done

    def drain(self, consumer: str, batch: int = 100) -> int:
        total = 0
        while self.lag(consumer) > 0:
            before = self.cursor(consumer)
            total += self.poll(consumer, batch)
            if self.cursor(consumer) == before:
                break
        return total
