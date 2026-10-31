"""The whole day, for everyone at once, and what happens when it changes.

``plan_day`` takes every visit booked for a day, gives each to the right technician by the hard rules and scores in ``assignment.py`` (skill, a unit in the van, shift, daily
cap; fix durability weighs more than speed; job difficulty is adjusted for), then plans each person's route inside the hours the shop is open. It starts from where the
person is now if their position is known (``live.py``), otherwise from their base. Nothing is guessed: a job nobody can take is returned UNASSIGNED with the reason per person,
and a route that cannot meet a shop's closing time reports how late it would be.

Two things can change the day, and both re-plan it in the same call:
  * SHOP_CLOSED     a person reaches a shop and it is shut. The visit leaves today's plan and comes back as a proposal for the shop's next opening.
  * RUNNING_LONG    a visit is taking longer. That person is busy until later; their remaining visits move, or go to someone who can take them.
Exceptions are recorded in ``visit_exceptions`` (one row each, ledgered) and plan_day reads them, so the plan is always rebuilt from the facts and is the same every time for
the same facts. Travel times come from an injected provider ``travel_min(origin, dest, departure)``: the real road providers in ``travel.py``, or the labelled SIMULATED one.

Not done here: booking the proposed new slot (that is an executor call: see ``apply_reschedule``), and the old booking is not cancelled (cancelling is a compensation, not an
agent action); the exception is what keeps it out of the plan.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from core.engine.clock import SimClock, iso
from field.assignment import Job, assign_from_db
from field.fix_ladder import apply_booking
from field.live import latest_positions
from field.profiles import load_techs
from field.routing import Stop, Vehicle, solve_routes
from field.shop_hours import get_hours

Point = tuple[float, float]
Travel = Callable[[Point, Point, datetime], float]
SCHEMA = ("CREATE TABLE IF NOT EXISTS visit_exceptions (exc_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, booking_id TEXT, person_id TEXT, "
          "minutes INTEGER, day TEXT NOT NULL, ts TEXT NOT NULL)")
KINDS = ("SHOP_CLOSED", "RUNNING_LONG")
SERVICE_MIN = 30                                   # [ASSUMPTION] minutes at a shop for a swap or a repair
MAX_DELAY_MIN = 240
DONE_STATUSES = ("CANCELLED", "DONE", "COMPLETED")
SKILL, ISSUE = "soundbox_repair", "soundbox"


@dataclass(frozen=True)
class PlannedStop:
    job_id: str
    merchant_id: str
    name: str
    arrive_min: int
    leave_min: int
    lat: float
    lng: float
    late_min: int


@dataclass
class DayPlan:
    day: str
    routes: dict[str, list[PlannedStop]] = field(default_factory=dict)
    unassigned: list[dict] = field(default_factory=list)
    rescheduled: list[dict] = field(default_factory=list)
    delayed: dict[str, int] = field(default_factory=dict)
    starts: dict[str, str] = field(default_factory=dict)         # person -> "live position" | "base"
    simulated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def _exceptions(conn: sqlite3.Connection, day: str) -> tuple[set[str], dict[str, int]]:
    try:
        rows = conn.execute("SELECT * FROM visit_exceptions WHERE day = ? ORDER BY exc_id", (day,)).fetchall()
    except sqlite3.OperationalError:
        return set(), {}
    closed = {r["booking_id"] for r in rows if r["kind"] == "SHOP_CLOSED"}
    delays: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "RUNNING_LONG":
            delays[r["person_id"]] = delays.get(r["person_id"], 0) + int(r["minutes"] or 0)
    return closed, delays


def record_exception(conn: sqlite3.Connection, clock: SimClock, kind: str, *, booking_id: str | None = None, person_id: str | None = None,
                     minutes: int | None = None, ledger=None) -> dict:
    """Record that a shop was closed or a visit is running long. Validated against the facts: the booking must be a visit, the person a technician."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {KINDS}")
    ensure_schema(conn)
    if kind == "SHOP_CLOSED":
        b = conn.execute("SELECT booking_id, slot_at, kind FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()
        if b is None or b["kind"] != "visit":
            raise KeyError(booking_id)
        day, person, mins = b["slot_at"][:10], None, None
    else:
        if conn.execute("SELECT 1 FROM technicians WHERE tech_id = ?", (person_id,)).fetchone() is None:
            raise KeyError(person_id)
        if not isinstance(minutes, int) or not (1 <= minutes <= MAX_DELAY_MIN):
            raise ValueError(f"minutes is a whole number from 1 to {MAX_DELAY_MIN}")
        day, person, mins = clock.now().date().isoformat(), person_id, minutes
    cur = conn.execute("INSERT INTO visit_exceptions(kind, booking_id, person_id, minutes, day, ts) VALUES (?,?,?,?,?,?)", (kind, booking_id, person, mins, day, clock.now_iso()))
    if ledger is not None:
        ledger.append(kind="STATE", actor="SYSTEM", payload={"field_day": kind, "booking_id": booking_id, "person_id": person, "minutes": mins})
    return {"exc_id": cur.lastrowid, "kind": kind, "booking_id": booking_id, "person_id": person, "minutes": mins, "day": day}


def _matrix(points: list[Point], travel: Travel, departure: datetime) -> list[list[int]]:
    return [[0 if i == j else math.ceil(travel(a, b, departure)) for j, b in enumerate(points)] for i, a in enumerate(points)]


def plan_day(conn: sqlite3.Connection, clock: SimClock, travel: Travel, *, day: str | None = None, now_min: int | None = None, persist: bool = False,
             simulated: bool = False) -> DayPlan:
    now = clock.now()
    today = now.date().isoformat()
    day = day or today
    now_min = now_min if now_min is not None else (now.hour * 60 + now.minute if day == today else 0)
    departure = now if day == today else datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=now.tzinfo)
    closed, delays = _exceptions(conn, day)
    plan = DayPlan(day, delayed=dict(delays), simulated=simulated)

    rows = conn.execute("SELECT b.booking_id, b.merchant_id, b.person_id, b.slot_at, m.name, m.lat, m.lng, m.language FROM bookings b JOIN merchants m ON m.merchant_id = b.merchant_id "
                        f"WHERE b.kind = 'visit' AND b.status NOT IN ({','.join('?' * len(DONE_STATUSES))}) AND b.slot_at LIKE ? ORDER BY b.slot_at, b.booking_id",
                        (*DONE_STATUSES, f"{day}%")).fetchall()
    techs = {t.tech_id: t for t in load_techs(conn)}
    jobs, info = [], {}
    for r in rows:
        hours = get_hours(conn, r["merchant_id"])
        if r["booking_id"] in closed:
            after = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=now.tzinfo) + timedelta(days=1)
            plan.rescheduled.append({"booking_id": r["booking_id"], "merchant_id": r["merchant_id"], "name": r["name"], "person_id": r["person_id"],
                                     "reason": "SHOP_CLOSED", "proposed_slot_at": iso(hours.next_open(after))})
            continue
        if r["lat"] is None or r["lng"] is None:
            plan.unassigned.append({"job_id": r["booking_id"], "merchant_id": r["merchant_id"], "reason": "NO_POSITION"})
            continue
        slot_day = datetime.fromisoformat(r["slot_at"])
        if not hours.is_open(slot_day.replace(hour=hours.open_min // 60, minute=hours.open_min % 60)):
            plan.unassigned.append({"job_id": r["booking_id"], "merchant_id": r["merchant_id"], "reason": "SHOP_CLOSED_THAT_DAY"})
            continue
        window = (hours.open_min, max(hours.open_min, hours.close_min - SERVICE_MIN))
        jobs.append(Job(r["booking_id"], r["merchant_id"], ISSUE, SKILL, {"soundbox_unit": 1}, language=r["language"] or "hi-IN", window=window,
                        owner_tech=r["person_id"] if r["person_id"] in techs else None, point=(r["lat"], r["lng"])))
        info[r["booking_id"]] = r

    positions = {p["person_id"]: p for p in latest_positions(conn, clock)} if day == today else {}
    by_tech: dict[str, list[Job]] = {}
    for a in (assign_from_db(conn, jobs) if jobs else []):
        if a.tech_id is None:
            plan.unassigned.append({"job_id": a.job_id, "merchant_id": info[a.job_id]["merchant_id"], "reason": a.reason, "rejected": a.rejected})
        else:
            by_tech.setdefault(a.tech_id, []).append(next(j for j in jobs if j.job_id == a.job_id))

    for tech_id in sorted(by_tech):
        t, mine = techs[tech_id], by_tech[tech_id]
        pos = positions.get(tech_id)
        start = (pos["lat"], pos["lng"]) if pos else t.base
        plan.starts[tech_id] = "live position" if pos else "base"
        points = [start] + [j.point for j in mine]
        stops = [Stop(j.job_id, i + 1, 1, SERVICE_MIN, j.window[0], j.window[1]) for i, j in enumerate(mine)]
        begin = max(t.shift[0], now_min + delays.get(tech_id, 0)) if day == today else t.shift[0]
        sol = solve_routes(_matrix(points, travel, departure), stops, [Vehicle(tech_id, 0, 0, begin, t.shift[1])], mode="service")
        visits = sol.routes.get(tech_id, [])
        plan.routes[tech_id] = [PlannedStop(v.stop_id, info[v.stop_id]["merchant_id"], info[v.stop_id]["name"], v.arrive, v.leave, info[v.stop_id]["lat"], info[v.stop_id]["lng"],
                                            int(sol.late.get(v.stop_id, 0))) for v in visits]
        for j in mine:
            if j.job_id not in {v.stop_id for v in visits}:
                plan.unassigned.append({"job_id": j.job_id, "merchant_id": j.merchant_id, "reason": "NO_FEASIBLE_ROUTE", "person_id": tech_id})
    if persist:
        _persist(conn, clock, plan)
    return plan


def _persist(conn: sqlite3.Connection, clock: SimClock, plan: DayPlan) -> None:
    for tech_id, stops in plan.routes.items():
        body = {"day": plan.day, "active": plan.day == clock.now().date().isoformat(), "simulated": plan.simulated, "built_at": clock.now_iso(), "start": plan.starts.get(tech_id),
                "stops": [asdict(s) for s in stops]}
        conn.execute("INSERT OR REPLACE INTO routes(route_id, person_id, person_kind, route_date, plan_json, created_at) VALUES (?,?,?,?,?,?)",
                     (f"R-{plan.day}-{tech_id}", tech_id, "tech", plan.day, json.dumps(body, sort_keys=True), clock.now_iso()))


def apply_reschedule(executor, proposal: dict, *, case_id: str | None = None):
    """Book the proposed new visit through the executor. The old booking stays on file; its exception keeps it out of the plan."""
    step = {"rung": 3, "booking": {"action": "book_visit_or_call", "args": {"merchant_id": proposal["merchant_id"], "kind": "visit", "slot_at": proposal["proposed_slot_at"],
                                                                          "person_id": proposal.get("person_id")}}}
    if step["booking"]["args"]["person_id"] is None:
        step["booking"]["args"].pop("person_id")
    return apply_booking(executor, step, case_id=case_id)
