"""The fix ladder: which rung to start a fault on, by weighing what each fix costs, how likely it is to work, and what the merchant loses while waiting.

    rung 1  Munim fixes it remotely or in chat      (restart, volume reset, settings push, then a test announcement)
    rung 2  an on-call human helper guides the merchant by voice or video
    rung 3  someone goes to the shop (a technician, or for a simple swap a courier or any certified person nearby)

Each rung has a cost, a chance of clearing the fault, and the hours it takes. If a rung fails the merchant climbs to the next, so starting on rung k costs

    E(k) = cost_k + loss_per_hour * hours_k + (1 - p_k) * E(k + 1)        (the last rung is treated as certain: nothing is above it)

and Munim starts on the rung with the lowest E. Nothing here is a rule that "repeat faults skip ahead": a repeat fault, or a device that is clearly broken, just makes the
cheap rungs unlikely to work, so starting on them costs more than starting higher. The skip falls out of the arithmetic, and the plan says which fact caused it.

Waiting loss per hour = a share of the merchant's daily volume (they lose sales and get disputes while the Soundbox cannot announce payments), spread over the hours
their shop is open. Every constant below is an [ASSUMPTION] for the synthetic world, in integer paise; none is a measurement, and the plan lists them with the result.
All the numbers are computed here, in code. Nothing a model said enters them.

``resolve_people`` turns the chosen rung into names: the helper, the courier or certified person, or the technician, each from its own rules (helpers.py, swap.py,
assignment.py). ``apply_booking`` books one through the executor; this module never calls a mock itself.
"""
from __future__ import annotations

import sqlite3
import zlib
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Callable

from core.actions.types import ActionIntent, Principal
from core.engine.clock import SimClock, iso, parse_iso
from field.assignment import Job, assign_from_db
from field.helpers import choose_helper
from field.shop_hours import get_hours
from field.swap import COURIER_COST_PAISE, choose_swap

Point = tuple[float, float]
PRINCIPAL = Principal("AGENT", "munim")
PLAYBOOK = "FIX_LADDER"

COST_PAISE = {1: 2_000, 2: 15_000, 3: 80_000}        # [ASSUMPTION] automation, a helper's quarter hour, a technician's trip
HOURS = {1: 0.25, 2: 1.0, 3: 24.0}                   # [ASSUMPTION] time to a result; a visit is next day
SWAP_HOURS = 6.0                                     # [ASSUMPTION] a courier or a nearby certified person
P_BASE = {1: 0.55, 2: 0.75, 3: 1.0}                  # [ASSUMPTION] chance each rung clears a Soundbox fault; the last rung is certain by definition
BROKEN_P = (0.03, 0.05)                              # [ASSUMPTION] a broken unit: a remote fix almost never works, and a call cannot repair hardware
REPEAT_P1 = 0.03                                     # [ASSUMPTION] a repeat means the last remote fix did not hold, so another one is very unlikely to
REPEAT_P2_FACTOR = 0.6                               # [ASSUMPTION] and a guided call is less likely to hold than it would be on a first fault
SILENT_BROKEN_HOURS = 48                             # no heartbeat this long and still not online: clearly broken
LOSS_SHARE_BP = 500                                  # [ASSUMPTION] 5% of a day's volume is lost or disputed while the device cannot announce, spread over open hours
RUNG_NAMES = {1: "Munim fixes it remotely or in chat", 2: "An on-call helper guides the merchant", 3: "Someone goes to the shop"}
SKILL, ISSUE = "soundbox_repair", "soundbox"


@dataclass(frozen=True)
class Situation:
    merchant_id: str
    language: str
    device_status: str
    last_test_ok: bool
    silent_hours: int | None
    remote_attempts: int
    replacements_30d: int
    chronic: bool
    daily_volume_paise: int
    open_hours: int
    hours_source: str


@dataclass(frozen=True)
class RungView:
    rung: int
    name: str
    cost_paise: int
    hours: float
    p_success: float
    expected_if_start_here_paise: int


@dataclass
class FixPlan:
    merchant_id: str
    start_rung: int                                  # 0 = nothing to fix
    rungs: list[RungView]
    loss_per_hour_paise: int
    swap: bool
    reasons: list[str]
    assumptions: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _count(conn: sqlite3.Connection, sql: str, args: tuple) -> int:
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:                 # a table that does not exist yet means nothing has happened there
        return 0


def read_situation(conn: sqlite3.Connection, clock: SimClock, merchant_id: str) -> Situation:
    m = conn.execute("SELECT merchant_id, language, avg_daily_volume_paise FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    if m is None:
        raise KeyError(merchant_id)
    d = conn.execute("SELECT * FROM devices WHERE merchant_id = ? ORDER BY device_id LIMIT 1", (merchant_id,)).fetchone()
    if d is None:
        raise LookupError("NO_DEVICE")
    now = clock.now()
    silent = max(0, int((now - parse_iso(d["last_heartbeat"])).total_seconds() // 3600)) if d["last_heartbeat"] else None
    since = iso(now - timedelta(days=30))
    hours = get_hours(conn, merchant_id)
    return Situation(
        merchant_id, m["language"], d["status"], bool(d["last_test_announcement_ok"]), silent,
        _count(conn, "SELECT COUNT(*) FROM device_remote_ops WHERE merchant_id = ? AND op != 'connectivity'", (merchant_id,)),
        _count(conn, "SELECT COUNT(*) FROM device_replacements WHERE merchant_id = ? AND status != 'CANCELLED' AND created_at >= ?", (merchant_id, since)),
        _count(conn, "SELECT COUNT(*) FROM chronic_flags WHERE merchant_id = ?", (merchant_id,)) > 0,
        int(m["avg_daily_volume_paise"] or 0), hours.open_hours, hours.source)


def build_plan(s: Situation) -> FixPlan:
    assumptions = {"cost_paise": COST_PAISE, "hours": HOURS, "swap_hours": SWAP_HOURS, "p_base": P_BASE, "loss_share_bp": LOSS_SHARE_BP,
                   "shop_hours": s.hours_source}
    loss = s.daily_volume_paise * LOSS_SHARE_BP // 10_000 // max(1, s.open_hours)
    reasons: list[str] = []
    if s.device_status == "ONLINE" and s.last_test_ok:
        return FixPlan(s.merchant_id, 0, [], loss, False, ["The device is online and its test announcement worked: there is nothing to fix."], assumptions)

    broken = s.device_status != "ONLINE" and ((s.device_status == "OFFLINE" and s.remote_attempts >= 1) or (s.silent_hours is not None and s.silent_hours >= SILENT_BROKEN_HOURS))
    repeat = s.replacements_30d >= 1 or s.chronic
    p1, p2 = P_BASE[1], P_BASE[2]
    if broken:
        p1, p2 = BROKEN_P
        reasons.append("The device is clearly broken: it is still offline after a remote fix" if s.remote_attempts >= 1 else
                       f"The device is clearly broken: no heartbeat for {s.silent_hours} hours")
    if repeat:
        p1, p2 = min(p1, REPEAT_P1), p2 * REPEAT_P2_FACTOR
        reasons.append("It is a repeat fault: " + ("a device was replaced in the last 30 days" if s.replacements_30d >= 1 else "it is flagged as chronic"))
    swap = broken and not repeat
    if swap:
        reasons.append("Nothing needs diagnosing, so a courier or a nearby certified person can swap the unit; no technician is needed")
    elif broken and repeat:
        reasons.append("A repeat fault needs a person who can find out why, so it is not treated as a simple swap")
    c3, h3 = (COURIER_COST_PAISE, SWAP_HOURS) if swap else (COST_PAISE[3], HOURS[3])
    c = {1: COST_PAISE[1], 2: COST_PAISE[2], 3: c3}
    h = {1: HOURS[1], 2: HOURS[2], 3: h3}
    p = {1: p1, 2: p2, 3: P_BASE[3]}
    e3 = c[3] + loss * h[3]
    e2 = c[2] + loss * h[2] + (1 - p[2]) * e3
    e1 = c[1] + loss * h[1] + (1 - p[1]) * e2
    expected = {1: e1, 2: e2, 3: e3}
    start = min((1, 2, 3), key=lambda k: (expected[k], k))
    if start > 1 and not (broken or repeat):
        reasons.append("Starting higher is cheaper than trying the lower rungs first")
    if start == 1 and not reasons:
        reasons.append("A remote fix is the cheapest way to start, and it is likely enough to work")
    rungs = [RungView(k, RUNG_NAMES[k], int(c[k]), h[k], round(p[k], 4), round(expected[k])) for k in (1, 2, 3)]
    return FixPlan(s.merchant_id, start, rungs, loss, swap, reasons, assumptions)


def plan_fix(conn: sqlite3.Connection, clock: SimClock, merchant_id: str) -> tuple[Situation, FixPlan]:
    s = read_situation(conn, clock, merchant_id)
    return s, build_plan(s)


def _slot(hours, arrive):
    """The first moment the shop is open at or after the person arrives."""
    return iso(hours.next_open(arrive))


def resolve_people(conn: sqlite3.Connection, clock: SimClock, s: Situation, plan: FixPlan, travel_min: Callable[[Point, Point], float]) -> dict:
    """Who does each rung from the start rung up. Every step that books something carries the exact ``book_visit_or_call`` arguments."""
    now = clock.now()
    steps: list[dict] = []
    if plan.start_rung == 0:
        return {"start_rung": 0, "steps": steps}
    if plan.start_rung <= 1:
        steps.append({"rung": 1, "who": "munim", "does": ["restart_device", "reset_device_volume", "push_device_config"], "then": "a test announcement must confirm it"})
    m = conn.execute("SELECT lat, lng FROM merchants WHERE merchant_id = ?", (s.merchant_id,)).fetchone()
    point = (m["lat"], m["lng"]) if m and m["lat"] is not None and m["lng"] is not None else None
    if plan.start_rung <= 2:
        got = choose_helper(conn, s.language, now, mode="voice")
        if got.choice:
            c = got.choice
            steps.append({"rung": 2, "who": "helper", "person_id": c.helper_id, "name": c.name, "mode": c.mode, "why": list(c.why),
                          "booking": {"action": "book_visit_or_call", "args": {"merchant_id": s.merchant_id, "kind": "call", "slot_at": iso(now), "person_id": c.helper_id}}})
        else:
            steps.append({"rung": 2, "who": None, "unavailable": "no helper is on call for this language right now", "rejected": got.rejected})
    hours = get_hours(conn, s.merchant_id)
    if point is None:
        steps.append({"rung": 3, "who": None, "unavailable": "the merchant has no position on record"})
        return {"start_rung": plan.start_rung, "steps": steps}
    if plan.swap:
        sw = choose_swap(conn, point, now, travel_min)
        if sw.choice:
            c = sw.choice
            steps.append({"rung": 3, "who": c.kind, "person_id": c.person_id, "name": c.name, "eta_min": c.eta_min, "cost_paise": c.cost_paise, "why": list(c.why),
                          "booking": {"action": "book_visit_or_call", "args": {"merchant_id": s.merchant_id, "kind": "visit", "slot_at": _slot(hours, now + timedelta(minutes=c.eta_min)),
                                                                              "person_id": c.person_id}}})
            return {"start_rung": plan.start_rung, "steps": steps}
        steps.append({"rung": 3, "note": "no courier or certified person can reach this shop in time: a technician goes instead", "rejected": sw.rejected})
    job = Job(f"FX-{s.merchant_id}", s.merchant_id, ISSUE, SKILL, {"soundbox_unit": 1}, language=s.language, point=point)
    a = assign_from_db(conn, [job])[0]
    if a.tech_id is None:
        steps.append({"rung": 3, "who": None, "unavailable": "no technician has the skill, a unit and a free slot", "rejected": a.rejected})
    else:
        t = conn.execute("SELECT name, base_lat, base_lng FROM technicians WHERE tech_id = ?", (a.tech_id,)).fetchone()
        eta = round(travel_min((t["base_lat"], t["base_lng"]), point))
        steps.append({"rung": 3, "who": "technician", "person_id": a.tech_id, "name": t["name"], "eta_min": eta, "score": a.score, "why": [f"{k.replace('_', ' ')} {v}" for k, v in a.parts.items()],
                      "booking": {"action": "book_visit_or_call", "args": {"merchant_id": s.merchant_id, "kind": "visit", "slot_at": _slot(hours, now + timedelta(hours=HOURS[3])),
                                                                          "person_id": a.tech_id}}})
    return {"start_rung": plan.start_rung, "steps": steps}


def apply_booking(executor, step: dict, *, case_id: str | None = None):
    """Book one step's visit or call through the executor (policy, ledger and an idempotency key as for any other action). Returns the executor's outcome."""
    b = step.get("booking")
    if not b:
        raise ValueError("this step has nothing to book")
    a = b["args"]
    seq = zlib.crc32(f"{a['merchant_id']}|{a['kind']}|{a['slot_at']}|{a.get('person_id')}".encode()) % 1_000_000     # deterministic: a retry is the same action
    return executor.submit(ActionIntent(b["action"], dict(a), case_id=case_id, step_seq=seq, principal=PRINCIPAL, playbook_id=PLAYBOOK, step=f"rung{step['rung']}",
                                        allowlist=(b["action"],)))
