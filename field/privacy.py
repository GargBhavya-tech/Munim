"""Field privacy and coaching (#113).

* Location is recorded ONLY while the person is inside their shift AND has an active route for today. Anything else is rejected, not
  stored and not queued (tracking outside the shift is the named mistake). Time comes from the injected SimClock.
* A person sees and can delete their own location points and coaching notes; nobody else's (managers get no raw location through this module).
* Call or visit analysis needs a GRANTED consent record on the ``call_analysis`` channel for that person (CONSENT-COACH-001). Without it
  nothing is analysed or stored. With it, the output is a COACHING note: strengths and suggestions. It has no score, no rank and no penalty
  field, by construction, so it cannot be used to sort or discipline people.
"""
from __future__ import annotations

import json
import sqlite3

from field.profiles import NotAllowed, load_rep, load_tech

SCHEMA = """
CREATE TABLE IF NOT EXISTS field_locations (
  loc_id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, route_id TEXT
);
CREATE TABLE IF NOT EXISTS coaching_notes (
  note_id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, visit_ref TEXT, created_at TEXT NOT NULL, strengths_json TEXT NOT NULL, suggestions_json TEXT NOT NULL
);
"""
ANALYSIS_CHANNEL = "call_analysis"          # packs/paytm_merchant/policy/limits.yaml: consent.analysis_channel (a test keeps them equal)
NOTE_FIELDS = ("note_id", "person_id", "visit_ref", "created_at", "strengths", "suggestions")


class LocationRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ConsentRequired(Exception):
    rule_id = "CONSENT-COACH-001"


class FieldPrivacy:
    def __init__(self, conn: sqlite3.Connection, clock, ledger=None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        conn.executescript(SCHEMA)

    # -- location -----------------------------------------------------------------------------------------------------
    def _shift(self, person_id: str, kind: str) -> tuple[int, int]:
        return (load_rep(self.conn, person_id) if kind == "rep" else load_tech(self.conn, person_id)).shift

    def _active_route(self, person_id: str, today: str) -> str | None:
        for r in self.conn.execute("SELECT route_id, plan_json FROM routes WHERE person_id = ? AND route_date = ?", (person_id, today)):
            if json.loads(r["plan_json"]).get("active") is True:
                return r["route_id"]
        return None

    def record_location(self, person_id: str, kind: str, lat: float, lng: float) -> int:
        now = self.clock.now()
        minute = now.hour * 60 + now.minute
        start, end = self._shift(person_id, kind)
        inside = start <= minute < end if start <= end else (minute >= start or minute < end)   # overnight shift wraps midnight
        if not inside:
            self._note(person_id, "REJECTED_OUTSIDE_SHIFT")
            raise LocationRejected("OUTSIDE_SHIFT")
        route = self._active_route(person_id, now.date().isoformat())
        if route is None:
            self._note(person_id, "REJECTED_NO_ACTIVE_ROUTE")
            raise LocationRejected("NO_ACTIVE_ROUTE")
        cur = self.conn.execute("INSERT INTO field_locations(person_id, kind, ts, lat, lng, route_id) VALUES (?,?,?,?,?,?)",
                                (person_id, kind, self.clock.now_iso(), lat, lng, route))
        return cur.lastrowid

    def _note(self, person_id: str, what: str) -> None:
        if self.ledger is not None:
            self.ledger.append(kind="STATE", actor="SYSTEM", payload={"field_privacy": what, "person": person_id})   # no coordinates in the note

    # -- the person's own data ----------------------------------------------------------------------------------------
    def own_data(self, person_id: str, viewer_id: str) -> dict:
        if viewer_id != person_id:
            raise NotAllowed("location and coaching data are visible to their owner only")
        locs = [dict(r) for r in self.conn.execute("SELECT loc_id, ts, lat, lng, route_id FROM field_locations WHERE person_id = ? ORDER BY loc_id", (person_id,))]
        notes = [self._note_row(r) for r in self.conn.execute("SELECT * FROM coaching_notes WHERE person_id = ? ORDER BY note_id", (person_id,))]
        return {"locations": locs, "coaching_notes": notes}

    def delete_location(self, loc_id: int, viewer_id: str) -> bool:
        row = self.conn.execute("SELECT person_id FROM field_locations WHERE loc_id = ?", (loc_id,)).fetchone()
        if row is None:
            return False
        if row["person_id"] != viewer_id:
            raise NotAllowed("only the owner may delete a location point")
        self.conn.execute("DELETE FROM field_locations WHERE loc_id = ?", (loc_id,))
        return True

    # -- coaching -----------------------------------------------------------------------------------------------------
    def _consented(self, person_id: str) -> bool:
        r = self.conn.execute("SELECT status FROM consent WHERE subject_id = ? AND channel = ?", (person_id, ANALYSIS_CHANNEL)).fetchone()
        return r is not None and r["status"] == "GRANTED"

    def analyse_visit(self, person_id: str, visit_ref: str, observations: dict) -> dict:
        """observations: {"strengths": [...], "suggestions": [...]} produced by the (consented) analysis. Stored as a coaching note."""
        if not self._consented(person_id):
            self._note(person_id, "ANALYSIS_REFUSED_NO_CONSENT")
            raise ConsentRequired(f"no {ANALYSIS_CHANNEL} consent for {person_id}")
        unknown = set(observations) - {"strengths", "suggestions"}
        if unknown:
            raise ValueError(f"a coaching note holds strengths and suggestions only, not {sorted(unknown)}")
        cur = self.conn.execute("INSERT INTO coaching_notes(person_id, visit_ref, created_at, strengths_json, suggestions_json) VALUES (?,?,?,?,?)",
                                (person_id, visit_ref, self.clock.now_iso(), json.dumps(observations.get("strengths", [])), json.dumps(observations.get("suggestions", []))))
        return self._note_row(self.conn.execute("SELECT * FROM coaching_notes WHERE note_id = ?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _note_row(r: sqlite3.Row) -> dict:
        return {"note_id": r["note_id"], "person_id": r["person_id"], "visit_ref": r["visit_ref"], "created_at": r["created_at"],
                "strengths": json.loads(r["strengths_json"]), "suggestions": json.loads(r["suggestions_json"])}
