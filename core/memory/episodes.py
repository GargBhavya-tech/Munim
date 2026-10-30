"""Verified case summaries (Bible section 33).

A summariser (injected callable, or the deterministic ledger-based fallback) returns sentences. Each sentence is a dict::

    {"text": "Replacement was delivered.", "cites": ["EV-12", "ledger:41"], "refs": ["replace_device", "DELIVERED"]}

``cites`` are event ids (events table) or ``ledger:<seq>`` for ledger entries of the same case. ``refs`` are the action /
status words the sentence relies on. The checker keeps a sentence only if it cites at least one thing, every cited item
exists AND belongs to this case, and every ref appears in the cited items' type/kind/payload. Anything else is dropped;
there is no "mostly right" path. Notes are English for staff.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from core.audit.ledger import canonical_json
from core.engine.clock import SimClock
from core.memory.profile import memory_allowed

Sentence = dict
Summariser = Callable[[str, list[dict]], list[Sentence]]


def _evidence(conn: sqlite3.Connection, case_id: str, cite: str) -> str | None:
    """Searchable lowercase text of a cited item, or None when it does not exist in this case."""
    if cite.startswith("ledger:"):
        try:
            seq = int(cite.split(":", 1)[1])
        except ValueError:
            return None
        r = conn.execute("SELECT kind, payload_json FROM ledger WHERE seq = ? AND case_id = ?", (seq, case_id)).fetchone()
        return f"{r['kind']} {r['payload_json']}".lower() if r else None
    r = conn.execute(
        "SELECT e.type, e.payload_json FROM events e WHERE e.event_id = ? AND (e.case_id = ? OR EXISTS "
        "(SELECT 1 FROM case_events ce WHERE ce.event_id = e.event_id AND ce.case_id = ?))", (cite, case_id, case_id)).fetchone()
    return f"{r['type']} {r['payload_json']}".lower() if r else None


def verify_sentences(conn: sqlite3.Connection, case_id: str, sentences: list[Sentence]) -> tuple[list[Sentence], list[Sentence]]:
    """Return (kept, dropped). Deterministic; no model involved."""
    kept, dropped = [], []
    for s in sentences:
        cites, refs = s.get("cites") or [], s.get("refs") or []
        ok = bool(str(s.get("text", "")).strip()) and bool(cites) and bool(refs)
        blob = ""
        if ok:
            ev = [_evidence(conn, case_id, str(c)) for c in cites]
            ok = all(e is not None for e in ev)
            blob = " ".join(e for e in ev if e)
        if ok:
            ok = all(str(r).lower() in blob for r in refs)
        (kept if ok else dropped).append(s)
    return kept, dropped


def ledger_fallback(conn: sqlite3.Connection, case_id: str) -> list[Sentence]:
    """Deterministic summary straight from the ledger: one sentence per action result / escalation / state change."""
    out: list[Sentence] = []
    for r in conn.execute("SELECT seq, kind, payload_json FROM ledger WHERE case_id = ? AND kind IN "
                          "('ACTION_RESULT','ESCALATION','STATE') ORDER BY seq", (case_id,)):
        p = json.loads(r["payload_json"])
        cite = f"ledger:{r['seq']}"
        if r["kind"] == "ACTION_RESULT" and p.get("action"):
            st = p.get("status", "?")
            out.append({"text": f"Action {p['action']} finished with status {st}.", "cites": [cite], "refs": [p["action"], st]})
        elif r["kind"] == "ESCALATION":
            reasons = p.get("reasons") or []
            out.append({"text": "Case was escalated to a human" + (f" ({', '.join(map(str, reasons))})" if reasons else "") + ".",
                        "cites": [cite], "refs": ["escalation"] + [str(x) for x in reasons]})
        elif r["kind"] == "STATE" and p.get("to"):
            out.append({"text": f"Case moved to {p['to']}.", "cites": [cite], "refs": [str(p["to"])]})
    return out


class EpisodeStore:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, summariser: Summariser | None = None):
        self.conn, self.clock, self.summariser = conn, clock, summariser

    def summarise_case(self, case_id: str) -> dict:
        """At case close or escalation. Stores ONLY verified sentences; returns the stored summary."""
        case = self.conn.execute("SELECT merchant_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if case is None:
            raise KeyError(case_id)
        merchant_id = case["merchant_id"]
        if merchant_id and not memory_allowed(self.conn, merchant_id):
            return {"sentences": [], "dropped": 0, "stored": False}
        facts = [dict(r) for r in self.conn.execute(
            "SELECT e.event_id, e.type, e.payload_json FROM case_events ce JOIN events e ON e.event_id = ce.event_id "
            "WHERE ce.case_id = ? ORDER BY e.seq", (case_id,))]
        raw = self.summariser(case_id, facts) if self.summariser else ledger_fallback(self.conn, case_id)
        kept, dropped = verify_sentences(self.conn, case_id, list(raw))
        summary = {"sentences": kept, "dropped": len(dropped), "language": "en"}
        self.conn.execute(
            "INSERT INTO episodes(episode_id, case_id, merchant_id, summary_json, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(episode_id) DO UPDATE SET summary_json=excluded.summary_json, created_at=excluded.created_at",
            (f"EP-{case_id}", case_id, merchant_id, canonical_json(summary), self.clock.now_iso()))
        return {**summary, "stored": True}

    def get(self, case_id: str) -> dict | None:
        r = self.conn.execute("SELECT summary_json FROM episodes WHERE case_id = ?", (case_id,)).fetchone()
        return json.loads(r["summary_json"]) if r else None

    def for_merchant(self, merchant_id: str) -> list[dict]:
        if not memory_allowed(self.conn, merchant_id):
            return []
        return [json.loads(r["summary_json"]) for r in self.conn.execute(
            "SELECT summary_json FROM episodes WHERE merchant_id = ? ORDER BY created_at, episode_id", (merchant_id,))]
