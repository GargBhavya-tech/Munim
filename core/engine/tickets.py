"""Ticket policy: raise fewer, merge only on proof, link on suspicion (Bible section 18).

This module is LOGIC around the ``tickets`` table. It never writes that table: tickets are created and withdrawn only
through the executor (``raise_ticket`` / ``withdraw_ticket``). Everything this module records (merges, links, families,
closures) lives in its own tables, created by ``ensure_schema``.

* ``should_raise_ticket`` - answer or fix in the conversation first; a ticket only when a human or field action is needed.
* ``classify_pair`` / ``TicketGraph`` - merge ONLY on the same system object; same merchant + similar text within 48 h is
  LINK ONLY (shown to a human). Tickets with no system object never merge.
* parent/child tickets for incidents; auto-close only on proof AND merchant confirmation; reopen within 7 days.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping

from core.engine.clock import SimClock, iso, parse_iso

LINK_WINDOW = timedelta(hours=48)
REOPEN_WINDOW = timedelta(days=7)
TEXT_SIMILARITY_MIN = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_merges (
  child_id TEXT PRIMARY KEY, primary_id TEXT NOT NULL, system_object TEXT NOT NULL, merged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_links (
  a_id TEXT NOT NULL, b_id TEXT NOT NULL, kind TEXT NOT NULL, similarity REAL, created_at TEXT NOT NULL,
  PRIMARY KEY (a_id, b_id)
);
CREATE TABLE IF NOT EXISTS ticket_family (
  child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, incident_id TEXT, linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_closures (
  ticket_id TEXT PRIMARY KEY, closed_at TEXT NOT NULL, reopened_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# ---------------------------------------------------------------- #90 fewer tickets
@dataclass(frozen=True)
class TicketDecision:
    raise_ticket: bool
    code: str
    reason: str


_HOW_TO = frozenset({"how_to", "howto", "information", "info", "question", "status_query"})


def _get(obj: Any, key: str, default=None):
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def should_raise_ticket(playbook_step: Any, case_facts: Mapping[str, Any]) -> TicketDecision:
    """Decide whether this step may raise a ticket. The answer always carries a reason.

    ``playbook_step``: a mapping or object with ``action`` (the step only ever raises a ticket when it is ``raise_ticket``).
    ``case_facts``: ``intent_kind`` (how_to/information/question/fault/...), ``fixed_in_conversation`` (bool),
    ``needs_human`` (bool), ``needs_field`` (bool), ``remote_steps_remaining`` (int).
    """
    action = _get(playbook_step, "action")
    if action != "raise_ticket":
        return TicketDecision(False, "NOT_A_TICKET_STEP", "this playbook step does not raise a ticket")
    kind = str(case_facts.get("intent_kind") or "").lower()
    if kind in _HOW_TO:
        return TicketDecision(False, "ANSWER_IN_CONVERSATION", "a how-to or information question is answered in the conversation, never ticketed")
    if case_facts.get("fixed_in_conversation"):
        return TicketDecision(False, "FIXED_IN_CONVERSATION", "the problem was fixed in the conversation")
    if case_facts.get("needs_human") or case_facts.get("needs_field"):
        who = "field action" if case_facts.get("needs_field") else "human action"
        return TicketDecision(True, "NEEDS_HUMAN_OR_FIELD", f"{who} is needed and the conversation cannot do it")
    if int(case_facts.get("remote_steps_remaining") or 0) > 0:
        return TicketDecision(False, "FIX_FIRST", "remote fix steps remain; fix in the conversation first")
    return TicketDecision(False, "NO_HUMAN_ACTION", "no human or field action is needed")


def ticket_rate_report(conn: sqlite3.Connection) -> dict:
    """Tickets per case (withdrawn tickets excluded), overall and per playbook. Reported, never enforced."""
    cases = {r[0]: r[1] for r in conn.execute("SELECT playbook_id, COUNT(*) FROM cases GROUP BY playbook_id")}
    tix = {r[0]: r[1] for r in conn.execute(
        "SELECT c.playbook_id, COUNT(*) FROM tickets t JOIN cases c ON c.case_id = t.case_id WHERE t.status != 'WITHDRAWN' GROUP BY c.playbook_id")}
    total_cases, total_tix = sum(cases.values()), sum(tix.values())
    by = {pb: {"cases": n, "tickets": tix.get(pb, 0), "rate": round(tix.get(pb, 0) / n, 4)} for pb, n in sorted(cases.items())}
    return {"cases": total_cases, "tickets": total_tix, "rate": round(total_tix / total_cases, 4) if total_cases else 0.0, "by_playbook": by}


# ---------------------------------------------------------------- #91 merge / link
def _tokens(text: str | None) -> set[str]:
    return {t for t in re.findall(r"[\w']+", (text or "").lower()) if len(t) > 2}


def text_similarity(a: str | None, b: str | None) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def classify_pair(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    """'merge' | 'link' | 'none'. Merge needs the SAME non-empty system object; text can only ever produce 'link'."""
    obj_a, obj_b = a.get("system_object"), b.get("system_object")
    if obj_a and obj_b and obj_a == obj_b:
        return "merge"
    if a.get("merchant_id") != b.get("merchant_id"):
        return "none"
    gap = abs(parse_iso(a["created_at"]) - parse_iso(b["created_at"]))
    if gap <= LINK_WINDOW and text_similarity(a.get("summary"), b.get("summary")) >= TEXT_SIMILARITY_MIN:
        return "link"
    return "none"


@dataclass(frozen=True)
class MergeResult:
    ticket_id: str
    merged_into: str | None
    merged_from: tuple[str, ...]
    linked_to: tuple[str, ...]


class TicketGraph:
    """Records merges, links, parent/child families and closures beside the tickets table (never in it)."""

    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock
        ensure_schema(conn)

    def _row(self, ticket_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if r is None:
            raise KeyError(ticket_id)
        return dict(r)

    def primary_of(self, ticket_id: str) -> str:
        seen = set()
        while True:
            r = self.conn.execute("SELECT primary_id FROM ticket_merges WHERE child_id=?", (ticket_id,)).fetchone()
            if r is None or ticket_id in seen:
                return ticket_id
            seen.add(ticket_id)
            ticket_id = r[0]

    def merged_children(self, primary_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT child_id FROM ticket_merges WHERE primary_id=? ORDER BY child_id", (primary_id,))]

    def links_of(self, ticket_id: str) -> list[str]:
        rows = self.conn.execute("SELECT a_id, b_id FROM ticket_links WHERE a_id=? OR b_id=?", (ticket_id, ticket_id)).fetchall()
        return sorted({r[1] if r[0] == ticket_id else r[0] for r in rows})

    def consider(self, ticket_id: str) -> MergeResult:
        """Compare a (new) ticket with the other live tickets: merge on the same system object, otherwise link on similar text."""
        me = self._row(ticket_id)
        others = self.conn.execute("SELECT * FROM tickets WHERE ticket_id != ? AND status NOT IN ('WITHDRAWN','CLOSED','RESOLVED') ORDER BY created_at, ticket_id",
                                   (ticket_id,)).fetchall()
        now = self.clock.now_iso()
        merged_into, merged_from, linked = None, [], []
        for o in (dict(x) for x in others):
            verdict = classify_pair(me, o)
            if verdict == "merge":
                # the earlier ticket is the primary; ties broken by id so the result does not depend on call order
                first, second = sorted((me, o), key=lambda t: (t["created_at"], t["ticket_id"]))
                primary = self.primary_of(first["ticket_id"])
                if second["ticket_id"] != primary and self.primary_of(second["ticket_id"]) != primary:
                    self.conn.execute("INSERT OR IGNORE INTO ticket_merges(child_id, primary_id, system_object, merged_at) VALUES (?,?,?,?)",
                                      (second["ticket_id"], primary, me["system_object"], now))
                if second["ticket_id"] == ticket_id:
                    merged_into = primary
                else:
                    merged_from.append(second["ticket_id"])
            elif verdict == "link":
                a, b = sorted((ticket_id, o["ticket_id"]))
                self.conn.execute("INSERT OR IGNORE INTO ticket_links(a_id, b_id, kind, similarity, created_at) VALUES (?,?,?,?,?)",
                                  (a, b, "SIMILAR_TEXT", round(text_similarity(me["summary"], o["summary"]), 4), now))
                linked.append(o["ticket_id"])
        self.conn.commit()
        return MergeResult(ticket_id, merged_into, tuple(sorted(merged_from)), tuple(sorted(linked)))

    # ----- parent / child (incidents)
    def attach_child(self, parent_id: str, child_id: str, incident_id: str | None = None) -> None:
        if parent_id == child_id:
            raise ValueError("a ticket cannot be its own parent")
        self.conn.execute("INSERT OR REPLACE INTO ticket_family(child_id, parent_id, incident_id, linked_at) VALUES (?,?,?,?)",
                          (child_id, parent_id, incident_id, self.clock.now_iso()))
        self.conn.commit()

    def children_of(self, parent_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT child_id FROM ticket_family WHERE parent_id=? ORDER BY child_id", (parent_id,))]

    def parent_cleared(self, parent_id: str, done_check: Callable[[str], bool]) -> dict[str, bool]:
        """The parent (incident) cleared: EACH child re-checks its OWN done-check. Nothing is closed from here."""
        return {cid: bool(done_check(cid)) for cid in self.children_of(parent_id)}

    # ----- auto-close and reopen
    def evaluate_close(self, ticket_id: str, *, done_check_passed: bool, merchant_confirmed: bool) -> TicketDecision:
        """Close only when the done-check passes AND the merchant confirms. A close decision is recorded for the reopen window."""
        if not done_check_passed:
            return TicketDecision(False, "DONE_CHECK_FAILED", "the done-check has not passed")
        if not merchant_confirmed:
            return TicketDecision(False, "AWAITING_CONFIRMATION", "done-check passed but the merchant has not confirmed")
        self.conn.execute("INSERT OR REPLACE INTO ticket_closures(ticket_id, closed_at, reopened_at) VALUES (?,?,NULL)", (ticket_id, self.clock.now_iso()))
        self.conn.commit()
        return TicketDecision(True, "PROVEN_AND_CONFIRMED", "done-check passed and the merchant confirmed")

    def evaluate_reopen(self, ticket_id: str, *, problem_returned: bool) -> TicketDecision:
        """Reopen automatically if the problem returns within 7 days of the close."""
        r = self.conn.execute("SELECT closed_at, reopened_at FROM ticket_closures WHERE ticket_id=?", (ticket_id,)).fetchone()
        if r is None:
            return TicketDecision(False, "NOT_AUTO_CLOSED", "this ticket was not auto-closed by proof")
        if not problem_returned:
            return TicketDecision(False, "NO_RECURRENCE", "the problem has not returned")
        if self.clock.now() - parse_iso(r[0]) > REOPEN_WINDOW:
            return TicketDecision(False, "WINDOW_PASSED", "the problem returned after the 7-day window: it is a new ticket, flagged as a repeat")
        self.conn.execute("UPDATE ticket_closures SET reopened_at=? WHERE ticket_id=?", (self.clock.now_iso(), ticket_id))
        self.conn.commit()
        return TicketDecision(True, "REOPEN", "the problem returned within 7 days of the close")
