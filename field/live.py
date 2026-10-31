"""Where field people are right now, as far as privacy allows.

Positions are recorded only inside a person's shift and with an active route for today (``field/privacy.py``): anything else is rejected and never stored. This module
shows the latest recorded position per person, and only a recent one: a point older than ``max_age_min`` is not "where they are right now", so it is left out rather
than shown stale. Nobody's history is returned here, only the latest point; the person sees and can delete their own history through ``FieldPrivacy``.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from core.engine.clock import SimClock, parse_iso
from field.privacy import FieldPrivacy, LocationRejected

MAX_AGE_MIN = 30


def latest_positions(conn: sqlite3.Connection, clock: SimClock, *, max_age_min: int = MAX_AGE_MIN) -> list[dict]:
    """The newest point of each person, if it is recent. Empty when nobody is on a route (or the table does not exist yet)."""
    try:
        rows = conn.execute("SELECT * FROM field_locations WHERE loc_id IN (SELECT MAX(loc_id) FROM field_locations GROUP BY person_id) ORDER BY person_id").fetchall()
    except sqlite3.OperationalError:
        return []
    now = clock.now()
    out = []
    for r in rows:
        age = (now - parse_iso(r["ts"])).total_seconds() / 60
        if age > max_age_min or age < -1:
            continue
        name = _name(conn, r["person_id"], r["kind"])
        out.append({"person_id": r["person_id"], "name": name, "kind": r["kind"], "lat": r["lat"], "lng": r["lng"], "ts": r["ts"], "age_min": max(0, int(age)), "route_id": r["route_id"]})
    return out


def _name(conn: sqlite3.Connection, person_id: str, kind: str) -> str | None:
    table, col = ("reps", "rep_id") if kind == "rep" else ("technicians", "tech_id")
    r = conn.execute(f"SELECT name FROM {table} WHERE {col} = ?", (person_id,)).fetchone()
    return r["name"] if r else None


def report_position(conn: sqlite3.Connection, clock: SimClock, ledger, person_id: str, kind: str, lat: float, lng: float) -> dict:
    """A person's phone reports where they are. Accepted only inside the shift and on an active route; otherwise the reason is returned and nothing is stored."""
    try:
        loc_id = FieldPrivacy(conn, clock, ledger).record_location(person_id, kind, lat, lng)
    except LocationRejected as e:
        return {"accepted": False, "reason": str(e)}
    return {"accepted": True, "loc_id": loc_id}
