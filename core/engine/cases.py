"""Case repository and state machine over the ``cases`` table.

Every transition writes its ledger STATE entry in the same transaction as the table update, and the
ledger is the source of truth: ``rebuild`` reconstructs a case's state from the ledger alone.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.audit.ledger import Ledger
from core.audit.replay import apply_entry, initial_state
from core.engine.clock import SimClock
from core.memory.store import next_id, write_txn

STATES = ("NEW", "ACTIVE", "WAITING", "PARKED", "NOT_NOW", "ESCALATED", "RETURNED_TO_AGENT", "DECLINED", "BLOCKED", "RESOLVED")
TERMINAL = frozenset({"RESOLVED", "DECLINED"})
LIVE = frozenset({"NEW", "ACTIVE", "WAITING"})  # the engine is actively working these

_ANY_OPEN_TARGETS = {"ESCALATED", "PARKED", "NOT_NOW", "DECLINED", "BLOCKED"}
LEGAL: dict[str, frozenset[str]] = {
    "NEW": frozenset(_ANY_OPEN_TARGETS | {"ACTIVE"}),
    "ACTIVE": frozenset(_ANY_OPEN_TARGETS | {"ACTIVE", "WAITING", "RESOLVED"}),
    "WAITING": frozenset(_ANY_OPEN_TARGETS | {"ACTIVE", "RESOLVED"}),
    "PARKED": frozenset(_ANY_OPEN_TARGETS - {"PARKED"} | {"ACTIVE"}),
    "NOT_NOW": frozenset(_ANY_OPEN_TARGETS - {"NOT_NOW"} | {"ACTIVE"}),
    "ESCALATED": frozenset({"RETURNED_TO_AGENT", "RESOLVED", "DECLINED"}),
    "RETURNED_TO_AGENT": frozenset(_ANY_OPEN_TARGETS - {"ESCALATED"} | {"ACTIVE"} | {"ESCALATED"}),
    "BLOCKED": frozenset({"ACTIVE", "ESCALATED", "RESOLVED", "DECLINED", "PARKED"}),
    "RESOLVED": frozenset(),
    "DECLINED": frozenset(),
}


class IllegalTransition(Exception):
    pass


@dataclass
class Case:
    case_id: str
    merchant_id: str | None
    lead_id: str | None
    playbook_id: str
    state: str
    lane: str
    language: str | None
    step: str | None
    touches: int
    opened_at: str
    closed_at: str | None
    resume_note: str | None

    @property
    def is_open(self) -> bool:
        return self.state not in TERMINAL

    @property
    def subject_id(self) -> str | None:
        return self.merchant_id or self.lead_id


def _row(r: sqlite3.Row) -> Case:
    return Case(r["case_id"], r["merchant_id"], r["lead_id"], r["playbook_id"], r["state"], r["lane"], r["language"],
                r["step"], r["touches"], r["opened_at"], r["closed_at"], r["resume_note"])


class CaseRepo:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger: Ledger):
        self.conn, self.clock, self.ledger = conn, clock, ledger

    def open_case(self, *, playbook_id: str, lane: str, merchant_id: str | None = None, lead_id: str | None = None,
                  language: str | None = None) -> Case:
        if not (merchant_id or lead_id):
            raise ValueError("a case needs a merchant or a lead")
        with write_txn(self.conn):
            cid = next_id(self.conn, "CS")
            now = self.clock.now_iso()
            self.ledger.append(kind="STATE", actor="SYSTEM", case_id=cid,
                               payload={"state": "NEW", "step": None, "playbook_id": playbook_id, "lane": lane, "reason": "opened"})
            self.conn.execute(
                "INSERT INTO cases(case_id, merchant_id, lead_id, playbook_id, state, lane, language, step, touches, opened_at)"
                " VALUES (?,?,?,?,?,?,?,?,0,?)", (cid, merchant_id, lead_id, playbook_id, "NEW", lane, language, None, now))
        return self.load(cid)

    def load(self, case_id: str) -> Case:
        r = self.conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if r is None:
            raise KeyError(case_id)
        return _row(r)

    def save(self, case: Case) -> None:
        """Persist the non-state mutable fields (state changes go through ``transition``)."""
        self.conn.execute("UPDATE cases SET language=?, touches=?, resume_note=? WHERE case_id=?",
                          (case.language, case.touches, case.resume_note, case.case_id))

    def transition(self, case: Case, new_state: str, *, step: str | None = None, reason: str | None = None,
                   event_id: str | None = None, resume_note: str | None = None) -> Case:
        """Ledger the STATE entry, then update the table, in one transaction. Illegal moves raise."""
        if new_state not in STATES:
            raise IllegalTransition(f"unknown state {new_state!r}")
        with write_txn(self.conn):
            cur = self.load(case.case_id)  # never trust a stale object
            if new_state not in LEGAL[cur.state]:
                raise IllegalTransition(f"{cur.case_id}: {cur.state} -> {new_state} is not allowed")
            new_step = step if step is not None else cur.step
            payload = {"state": new_state, "step": new_step, "from": cur.state}
            if reason:
                payload["reason"] = reason
            if event_id:
                payload["event_id"] = event_id
            self.ledger.append(kind="STATE", actor="SYSTEM", case_id=cur.case_id, payload=payload)
            closed = self.clock.now_iso() if new_state in TERMINAL else cur.closed_at
            note = resume_note if resume_note is not None else (None if new_state == "ACTIVE" else cur.resume_note)
            self.conn.execute("UPDATE cases SET state=?, step=?, closed_at=?, resume_note=? WHERE case_id=?",
                              (new_state, new_step, closed, note, cur.case_id))
        return self.load(case.case_id)

    def link_event(self, case_id: str, event_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO case_events(case_id, event_id) VALUES (?,?)", (case_id, event_id))

    def cases_for(self, merchant_id: str) -> list[Case]:
        rows = self.conn.execute("SELECT * FROM cases WHERE merchant_id = ? OR lead_id = ? ORDER BY opened_at, case_id",
                                 (merchant_id, merchant_id)).fetchall()
        return [_row(r) for r in rows]

    def open_cases(self, merchant_id: str) -> list[Case]:
        return [c for c in self.cases_for(merchant_id) if c.is_open]

    def last_activity(self, case_id: str) -> int:
        r = self.conn.execute("SELECT MAX(e.seq) FROM case_events ce JOIN events e ON e.event_id = ce.event_id WHERE ce.case_id = ?",
                              (case_id,)).fetchone()
        return r[0] or 0

    def rebuild(self, case_id: str) -> dict:
        """Event-sourced state from the ledger only: {'status', 'step', 'entries'}."""
        state = initial_state()
        entries = self.ledger.entries(case_id)
        for e in entries:
            apply_entry(state, e)
        return {"status": state["status"], "step": state["step"], "entries": len(entries), "state": state}
