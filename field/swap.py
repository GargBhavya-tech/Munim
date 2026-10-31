"""A swap without a technician: a courier, or any certified person nearby.

When the device is clearly broken and nothing needs diagnosing (it is simply replaced), the unit does not have to go with a technician. A courier carrying a unit
can bring it, and so can any certified person who is close and has a unit on them. The cheapest of those who can arrive in time is chosen, by code, and the choice
says who was passed over and why. A repeat fault is NOT a simple swap: it needs a person who can find out why (see ``field/fix_ladder.py``).

``certified`` means the person's profile lists a swap certification and a unit in their parts. Couriers are SYNTHETIC. Reads are safe on a read-only connection.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from field.profiles import load_techs
from field.shop_hours import to_minutes

Point = tuple[float, float]
SCHEMA = ("CREATE TABLE IF NOT EXISTS couriers (courier_id TEXT PRIMARY KEY, name TEXT NOT NULL, base_lat REAL NOT NULL, base_lng REAL NOT NULL, "
          "shift_start TEXT NOT NULL, shift_end TEXT NOT NULL, units INTEGER NOT NULL)")
COURIERS = [                                                # SYNTHETIC: (id, name, base lat, base lng, shift from, shift to, units carried)
    ("CR-01", "Vikram (courier)", 12.95, 77.60, "08:00", "20:00", 6),
    ("CR-02", "Salma (courier)", 12.99, 77.70, "09:00", "21:00", 6),
    ("CR-03", "Manjunath (courier)", 15.36, 75.10, "08:00", "19:00", 4),
]
SWAP_CERTIFICATIONS = frozenset({"soundbox_l1"})
UNIT = "soundbox_unit"
COURIER_COST_PAISE = 35_000            # [ASSUMPTION] a courier trip
CERTIFIED_COST_PAISE = 80_000          # [ASSUMPTION] a technician's trip
MAX_TRAVEL_MIN = 45


@dataclass(frozen=True)
class SwapChoice:
    kind: str                           # courier | certified_person
    person_id: str
    name: str
    eta_min: int
    cost_paise: int
    why: tuple[str, ...]


@dataclass
class SwapResult:
    choice: SwapChoice | None
    rejected: dict = field(default_factory=dict)            # person id -> why not


def ensure_couriers(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    for row in COURIERS:
        conn.execute("INSERT OR IGNORE INTO couriers VALUES (?,?,?,?,?,?,?)", row)


def _couriers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute("SELECT * FROM couriers ORDER BY courier_id").fetchall()
    except sqlite3.OperationalError:
        return []


def choose_swap(conn: sqlite3.Connection, point: Point, at: datetime, travel_min: Callable[[Point, Point], float], *, max_travel_min: int = MAX_TRAVEL_MIN) -> SwapResult:
    """Cheapest person who can bring a unit to ``point`` within ``max_travel_min``, on shift at ``at``. Ties: the quicker, then the id."""
    minute = at.hour * 60 + at.minute
    options, rejected = [], {}
    for c in _couriers(conn):
        if c["units"] < 1:
            rejected[c["courier_id"]] = "NO_UNIT"
        elif not (to_minutes(c["shift_start"]) <= minute < to_minutes(c["shift_end"])):
            rejected[c["courier_id"]] = "OFF_SHIFT"
        else:
            eta = round(travel_min((c["base_lat"], c["base_lng"]), point))
            if eta > max_travel_min:
                rejected[c["courier_id"]] = "TOO_FAR"
            else:
                options.append((COURIER_COST_PAISE, eta, c["courier_id"], "courier", c["name"]))
    for t in load_techs(conn):
        if not (SWAP_CERTIFICATIONS & set(t.certifications)):
            rejected[t.tech_id] = "NOT_CERTIFIED"
        elif t.parts.get(UNIT, 0) < 1:
            rejected[t.tech_id] = "NO_UNIT"
        elif not (t.shift[0] <= minute < t.shift[1]):
            rejected[t.tech_id] = "OFF_SHIFT"
        else:
            eta = round(travel_min(t.base, point))
            if eta > max_travel_min:
                rejected[t.tech_id] = "TOO_FAR"
            else:
                options.append((CERTIFIED_COST_PAISE, eta, t.tech_id, "certified_person", t.name))
    if not options:
        return SwapResult(None, rejected)
    cost, eta, pid, kind, name = min(options, key=lambda o: (o[0], o[1], o[2]))
    why = (f"{'a courier with a unit' if kind == 'courier' else 'certified for swaps, with a unit on them'}", f"about {eta} minutes away", f"{len(options) - 1} other option(s) cost more or are further")
    return SwapResult(SwapChoice(kind, pid, name, eta, cost, why), rejected)
