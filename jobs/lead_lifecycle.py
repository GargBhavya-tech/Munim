"""Lead lifecycle (#101; Bible section 7): the states a new-merchant lead moves through before any case exists.

    NEW -> RESEARCHED -> CONTACTED -> ENGAGED -> QUALIFIED -> HANDED_OFF -> WON        (WON opens an S1 activation case)
           CONTACTED / ENGAGED -> NO_REPLY | NOT_NOW -> NURTURE -> CONTACTED (retry)
           any open state -> DISQUALIFIED | DO_NOT_CONTACT

* ``WON`` is NOT the sale. A sale is done at the first settlement, which is S1's done-check. So a WON lead stays OPEN until its S1 case
  resolves (``status`` says WON_PENDING_FIRST_SETTLEMENT, then ACTIVATED); closing the lead on WON is the named mistake.
* A lead leaves NEW only if dedupe cleared it (``outreach_ready``): an existing merchant, a duplicate, or a lead held for review cannot start.
* DO_NOT_CONTACT is final: the number joins ``dnd_registry``, pending timers are cancelled, and no later transition is possible.
* Every transition is validated against the table below, ledgered, and recorded in ``lead_transitions``; an illegal one raises.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.engine.clock import SimClock
from core.memory.store import write_txn

STATES = ("NEW", "RESEARCHED", "CONTACTED", "ENGAGED", "QUALIFIED", "HANDED_OFF", "WON", "NO_REPLY", "NOT_NOW", "NURTURE", "DISQUALIFIED", "DO_NOT_CONTACT")
TERMINAL = frozenset({"DISQUALIFIED", "DO_NOT_CONTACT"})
_STOP = {"DISQUALIFIED", "DO_NOT_CONTACT"}
TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW": frozenset({"RESEARCHED"} | _STOP),
    "RESEARCHED": frozenset({"CONTACTED"} | _STOP),
    "CONTACTED": frozenset({"ENGAGED", "NO_REPLY", "NOT_NOW"} | _STOP),
    "ENGAGED": frozenset({"QUALIFIED", "NO_REPLY", "NOT_NOW"} | _STOP),
    "QUALIFIED": frozenset({"HANDED_OFF", "WON", "NOT_NOW"} | _STOP),
    "HANDED_OFF": frozenset({"WON", "NOT_NOW"} | _STOP),
    "NO_REPLY": frozenset({"CONTACTED", "NURTURE"} | _STOP),
    "NOT_NOW": frozenset({"NURTURE"} | _STOP),
    "NURTURE": frozenset({"CONTACTED"} | _STOP),
    "WON": frozenset({"DO_NOT_CONTACT"}),
    "DISQUALIFIED": frozenset(),
    "DO_NOT_CONTACT": frozenset(),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_transitions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, reason TEXT, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_cases (lead_id TEXT NOT NULL, case_id TEXT NOT NULL, playbook_id TEXT NOT NULL, PRIMARY KEY (lead_id, case_id))
"""


class IllegalTransition(ValueError):
    pass


class NotCleared(ValueError):
    """The lead has not passed duplicate matching (or matched a merchant): it may not leave NEW."""


@dataclass(frozen=True)
class Lead:
    lead_id: str
    state: str
    status: str                 # the state, or WON_PENDING_FIRST_SETTLEMENT / ACTIVATED for a won lead
    open: bool


class LeadLifecycle:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, engine=None, timers=None):
        self.conn, self.clock, self.ledger, self.engine, self.timers = conn, clock, ledger, engine, timers
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)

    # ------------------------------------------------------------------------------------------------------ reads
    def _row(self, lead_id: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if r is None:
            raise KeyError(lead_id)
        return r

    def s1_case(self, lead_id: str):
        r = self.conn.execute("SELECT case_id FROM lead_cases WHERE lead_id = ? AND playbook_id = 'S1_ACTIVATION' ORDER BY rowid DESC", (lead_id,)).fetchone()
        return self.engine.repo.load(r["case_id"]) if (r and self.engine) else None

    def get(self, lead_id: str) -> Lead:
        state = self._row(lead_id)["state"]
        if state == "WON":
            case = self.s1_case(lead_id)
            done = case is not None and case.state == "RESOLVED"
            return Lead(lead_id, state, "ACTIVATED" if done else "WON_PENDING_FIRST_SETTLEMENT", not done)
        return Lead(lead_id, state, state, state not in TERMINAL)

    # ------------------------------------------------------------------------------------------------------ moves
    def move(self, lead_id: str, to: str, reason: str | None = None) -> Lead:
        row = self._row(lead_id)
        cur = row["state"]
        if to not in STATES or to not in TRANSITIONS.get(cur, frozenset()):
            raise IllegalTransition(f"{lead_id}: {cur} -> {to} is not allowed")
        if cur == "NEW" and to == "RESEARCHED" and not self._cleared(row):
            raise NotCleared(f"{lead_id} has not been cleared by duplicate matching (state {cur}, merchant {row['matched_merchant_id']})")
        with write_txn(self.conn):
            self.conn.execute("UPDATE leads SET state = ? WHERE lead_id = ?", (to, lead_id))
            self.conn.execute("INSERT INTO lead_transitions(lead_id, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)", (lead_id, cur, to, reason, self.clock.now_iso()))
            self.ledger.append(kind="STATE", actor="SYSTEM", payload={"lead_id": lead_id, "from": cur, "state": to, "reason": reason})
            if to == "DO_NOT_CONTACT":
                self._stop_contact(row)
        return self.get(lead_id)

    @staticmethod
    def _cleared(row: sqlite3.Row) -> bool:
        return row["matched_merchant_id"] is None and '"result": "CLEAR"' in (row["provenance_json"] or "")

    def _stop_contact(self, row: sqlite3.Row) -> None:
        if row["phone_public"]:
            self.conn.execute("INSERT OR IGNORE INTO dnd_registry(phone) VALUES (?)", (row["phone_public"],))
        if self.timers is not None:
            self.timers.cancel_for_subject(row["lead_id"], reason="do_not_contact")

    # ------------------------------------------------------------------------------------------------------ WON -> S1
    def win(self, lead_id: str, merchant_id: str, language: str | None = None) -> Lead:
        """The merchant agreed. Records the new merchant id, moves the lead to WON and opens its S1 case. The lead stays OPEN until S1 resolves."""
        if self.engine is None:
            raise RuntimeError("winning a lead opens an S1 case: the lifecycle needs the engine")
        self.move(lead_id, "WON", "agreed")
        with write_txn(self.conn):
            self.conn.execute("UPDATE leads SET matched_merchant_id = ? WHERE lead_id = ?", (merchant_id, lead_id))
        case = self.engine.open_case(merchant_id, "S1_ACTIVATION", lead_id=lead_id, language=language)
        with write_txn(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO lead_cases(lead_id, case_id, playbook_id) VALUES (?,?, 'S1_ACTIVATION')", (lead_id, case.case_id))
            self.ledger.append(kind="STATE", actor="SYSTEM", case_id=case.case_id, payload={"lead_id": lead_id, "opened_from": "WON", "merchant_id": merchant_id})
        return self.get(lead_id)

    def history(self, lead_id: str) -> list[tuple[str, str]]:
        return [(r["from_state"], r["to_state"]) for r in self.conn.execute("SELECT from_state, to_state FROM lead_transitions WHERE lead_id = ? ORDER BY seq", (lead_id,))]


__all__ = ["LeadLifecycle", "Lead", "IllegalTransition", "NotCleared", "TRANSITIONS", "STATES"]
