"""Timers over the ``timers`` table. A timer fires once, as a TIMER_FIRED event, and never after its case closes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Callable

from core.audit.ledger import Ledger
from core.engine.cases import TERMINAL
from core.engine.clock import SimClock, iso, parse_iso
from core.engine.events import append_event
from core.memory.store import write_txn


class TimerService:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger: Ledger, handler: Callable[[dict], object] | None = None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        self.handler = handler
        self._handles: dict[int, int] = {}

    def set_handler(self, handler: Callable[[dict], object]) -> None:
        self.handler = handler

    # -- scheduling ----------------------------------------------------------------
    def schedule(self, case_id: str, due_at: datetime, kind: str, payload: dict | None = None) -> int:
        with write_txn(self.conn):
            cur = self.conn.execute("INSERT INTO timers(case_id, kind, due_at, status, payload_json) VALUES (?,?,?,'PENDING',?)",
                                    (case_id, kind, iso(due_at), json.dumps(payload or {}, sort_keys=True)))
            tid = cur.lastrowid
        self._arm(tid, due_at)
        return tid

    def _arm(self, timer_id: int, due: datetime) -> None:
        self._handles[timer_id] = self.clock.call_at(due, lambda: self._fire(timer_id))

    def rearm(self) -> int:
        """After a restart: re-register every pending timer with the clock. Overdue ones fire now."""
        rows = self.conn.execute("SELECT timer_id, due_at FROM timers WHERE status = 'PENDING' ORDER BY due_at, timer_id").fetchall()
        n = 0
        for r in rows:
            due = parse_iso(r["due_at"])
            if r["timer_id"] in self._handles:
                continue
            if due <= self.clock.now():
                self._fire(r["timer_id"])
            else:
                self._arm(r["timer_id"], due)
            n += 1
        return n

    # -- firing --------------------------------------------------------------------
    def _fire(self, timer_id: int) -> None:
        self._handles.pop(timer_id, None)
        with write_txn(self.conn):
            t = self.conn.execute("SELECT t.*, c.state AS cstate, c.merchant_id AS mid FROM timers t "
                                  "LEFT JOIN cases c ON c.case_id = t.case_id WHERE t.timer_id = ? AND t.status = 'PENDING'",
                                  (timer_id,)).fetchone()
            if t is None:
                return  # already fired or cancelled: exactly once
            if t["cstate"] in TERMINAL:
                self._cancel_row(t["timer_id"], t["case_id"], t["kind"], "case_closed")
                return
            self.conn.execute("UPDATE timers SET status = 'FIRED' WHERE timer_id = ?", (timer_id,))
            payload = {"timer_id": timer_id, "kind": t["kind"], **json.loads(t["payload_json"] or "{}")}
            event = {"event_id": f"TMR-{timer_id}", "type": "TIMER_FIRED", "merchant_id": t["mid"], "case_id": t["case_id"], "payload": payload}
            append_event(self.conn, event_id=event["event_id"], type="TIMER_FIRED", payload=payload,
                         ts_sim=self.clock.now_iso(), merchant_id=t["mid"], case_id=t["case_id"])
        if self.handler:
            self.handler(event)

    # -- cancelling (an R1 compensation, ledgered) -----------------------------------
    def _cancel_row(self, timer_id: int, case_id: str, kind: str, reason: str) -> None:
        self.conn.execute("UPDATE timers SET status = 'CANCELLED' WHERE timer_id = ? AND status = 'PENDING'", (timer_id,))
        h = self._handles.pop(timer_id, None)
        if h is not None:
            self.clock.cancel(h)
        self.ledger.append(kind="COMPENSATION", actor="SYSTEM", case_id=case_id,
                           payload={"action": "cancel_timer", "status": "CANCELLED", "timer_id": timer_id, "timer_kind": kind, "reason": reason})

    def cancel(self, timer_id: int, reason: str = "cancelled") -> bool:
        with write_txn(self.conn):
            t = self.conn.execute("SELECT * FROM timers WHERE timer_id = ? AND status = 'PENDING'", (timer_id,)).fetchone()
            if t is None:
                return False
            self._cancel_row(timer_id, t["case_id"], t["kind"], reason)
        return True

    def cancel_for_case(self, case_id: str, kind: str | None = None, reason: str = "case_closed") -> int:
        q, a = "SELECT * FROM timers WHERE case_id = ? AND status = 'PENDING'", [case_id]
        if kind:
            q, a = q + " AND kind = ?", a + [kind]
        with write_txn(self.conn):
            rows = self.conn.execute(q + " ORDER BY timer_id", a).fetchall()
            for t in rows:
                self._cancel_row(t["timer_id"], t["case_id"], t["kind"], reason)
        return len(rows)

    def cancel_for_subject(self, subject_id: str, reason: str = "do_not_contact") -> int:
        with write_txn(self.conn):
            rows = self.conn.execute(
                "SELECT t.* FROM timers t JOIN cases c ON c.case_id = t.case_id "
                "WHERE t.status = 'PENDING' AND (c.merchant_id = ? OR c.lead_id = ?) ORDER BY t.timer_id",
                (subject_id, subject_id)).fetchall()
            for t in rows:
                self._cancel_row(t["timer_id"], t["case_id"], t["kind"], reason)
        return len(rows)

    def pending(self, case_id: str | None = None) -> list[sqlite3.Row]:
        if case_id:
            return self.conn.execute("SELECT * FROM timers WHERE status='PENDING' AND case_id=? ORDER BY timer_id", (case_id,)).fetchall()
        return self.conn.execute("SELECT * FROM timers WHERE status='PENDING' ORDER BY timer_id").fetchall()
