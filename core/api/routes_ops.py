"""Service operations: the fix ladder, the field day, who is on call, shop hours and the scam sandbox (docs/API_CONTRACT.md).

Reads compute from the database and the job modules (``field/fix_ladder.py``, ``field/day_plan.py``, ``field/helpers.py``, ``field/swap.py``, ``field/live.py``,
``packs/paytm_merchant/scam_sandbox.py``); nothing here decides anything itself. The few writes are the small ones a person does on the field day (a shop was closed, a visit
is running long, reported shop hours) and booking a proposed new visit, which goes through the executor like every other side effect. Travel times come from the labelled
SIMULATED road provider in the demo, and every response that depends on them says ``simulated: true``.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.deps import read_db, write_db
from core.audit.ledger import Ledger
from core.memory.store import write_txn
from field.day_plan import apply_reschedule, plan_day, record_exception
from field.fix_ladder import apply_booking, plan_fix, resolve_people
from field.helpers import load_helpers
from field.live import latest_positions, report_position
from field.profiles import load_techs
from field.shop_hours import get_hours, set_hours
from field.simulated_travel import SimulatedRoadProvider
from field.swap import _couriers
from packs.paytm_merchant.scam_sandbox import Claim, check_claim

router = APIRouter(prefix="/api/ops")
_ROAD = SimulatedRoadProvider()


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _hours_json(h) -> dict:
    return {"open": _hhmm(h.open_min), "close": _hhmm(h.close_min), "closed_days": list(h.closed_days), "source": h.source}


# ---------------------------------------------------------------------------------------------------- the fix ladder
@router.get("/fix-plan/{merchant_id}")
def fix_plan(merchant_id: str, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):                 # looking people up creates their own tables on first use; it writes no merchant data
        try:
            s, plan = plan_fix(conn, clock, merchant_id)
        except KeyError:
            raise HTTPException(404, "MERCHANT_NOT_FOUND")
        except LookupError:
            raise HTTPException(404, "NO_DEVICE")
        people = resolve_people(conn, clock, s, plan, lambda a, b: _ROAD.minutes(a, b, clock.now()))
    return {"situation": asdict(s), "plan": plan.as_dict(), "people": people, "simulated": True}


class BookIn(BaseModel):
    rung: int = Field(ge=2, le=3)


@router.post("/fix-plan/{merchant_id}/book")
def fix_plan_book(merchant_id: str, body: BookIn, request: Request):
    """Book the helper call (rung 2) or the visit (rung 3) the plan names, through the executor. Rung 1 is Munim's own and books nothing."""
    clock, executor = request.app.state.clock, request.app.state.executor
    with write_db(request) as conn, write_txn(conn):
        try:
            s, plan = plan_fix(conn, clock, merchant_id)
        except KeyError:
            raise HTTPException(404, "MERCHANT_NOT_FOUND")
        except LookupError:
            raise HTTPException(404, "NO_DEVICE")
        steps = resolve_people(conn, clock, s, plan, lambda a, b: _ROAD.minutes(a, b, clock.now()))["steps"]
    step = next((x for x in steps if x.get("rung") == body.rung and x.get("booking")), None)
    if step is None:
        raise HTTPException(409, {"error": "NOTHING_TO_BOOK", "reason": "the plan has no person for this rung right now"})
    out = apply_booking(executor, step)
    return {"status": out.status, "error": out.error, "action_id": out.action_id, "step": step}


# ---------------------------------------------------------------------------------------------------- the field day
@router.get("/day")
def day(request: Request, day: Optional[str] = None):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        plan = plan_day(conn, clock, _ROAD.minutes, day=day, simulated=True)
    return plan.as_dict()


class ExceptionIn(BaseModel):
    kind: str = Field(pattern="^(SHOP_CLOSED|RUNNING_LONG)$")
    booking_id: Optional[str] = None
    person_id: Optional[str] = None
    minutes: Optional[int] = None


@router.post("/day/exception")
def day_exception(body: ExceptionIn, request: Request):
    """A shop was closed, or a visit is running long. Recorded, ledgered, and the day is re-planned in the same call."""
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        try:
            exc = record_exception(conn, clock, body.kind, booking_id=body.booking_id, person_id=body.person_id, minutes=body.minutes, ledger=Ledger(conn, clock))
        except KeyError as e:
            raise HTTPException(404, {"error": "NOT_FOUND", "reason": f"{e.args[0]} is not known"})
        except ValueError as e:
            raise HTTPException(422, {"error": "BAD_EXCEPTION", "reason": str(e)})
        Ledger(conn, clock).append(kind="HUMAN_ACTION", actor="HUMAN:operator", role="ops_admin", payload={"action": "field_day_exception", **{k: v for k, v in exc.items() if k != "exc_id"}})
        plan = plan_day(conn, clock, _ROAD.minutes, day=exc["day"], persist=True, simulated=True)
    return {"exception": exc, "plan": plan.as_dict()}


class RescheduleIn(BaseModel):
    booking_id: str


@router.post("/day/reschedule")
def day_reschedule(body: RescheduleIn, request: Request):
    """Book the proposed new visit for a shop that was closed. It goes through the executor (policy, ledger, an idempotency key)."""
    clock, executor = request.app.state.clock, request.app.state.executor
    with write_db(request) as conn:
        plans = plan_day(conn, clock, _ROAD.minutes, day=_booking_day(conn, body.booking_id), simulated=True)
    proposal = next((p for p in plans.rescheduled if p["booking_id"] == body.booking_id), None)
    if proposal is None:
        raise HTTPException(409, {"error": "NOTHING_TO_RESCHEDULE", "reason": "this visit has not been reported as a closed shop"})
    out = apply_reschedule(executor, proposal)
    return {"status": out.status, "error": out.error, "action_id": out.action_id, "proposal": proposal}


def _booking_day(conn, booking_id: str) -> str:
    r = conn.execute("SELECT slot_at FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "BOOKING_NOT_FOUND")
    return r["slot_at"][:10]


# ---------------------------------------------------------------------------------------------------- people
@router.get("/people")
def people(request: Request, conn=Depends(read_db)):
    clock = request.app.state.clock
    techs = [{"tech_id": t.tech_id, "name": t.name, "languages": t.languages, "skills": sorted(t.skills), "certifications": list(t.certifications), "parts": t.parts,
              "shift": [_hhmm(t.shift[0]), _hhmm(t.shift[1])], "base": list(t.base), "max_jobs_per_day": t.max_jobs_per_day} for t in load_techs(conn)]
    helpers = [{"helper_id": h.helper_id, "name": h.name, "languages": h.languages, "modes": list(h.modes), "on_call": [_hhmm(h.on_call[0]), _hhmm(h.on_call[1])],
                "skills": sorted(h.skills)} for h in load_helpers(conn)]
    couriers = [{"courier_id": c["courier_id"], "name": c["name"], "base": [c["base_lat"], c["base_lng"]], "shift": [c["shift_start"], c["shift_end"]], "units": c["units"]}
                for c in _couriers(conn)]
    return {"technicians": techs, "helpers": helpers, "couriers": couriers, "positions": latest_positions(conn, clock), "sim_time": clock.now_iso()}


class PositionIn(BaseModel):
    kind: str = Field(default="tech", pattern="^(tech|rep)$")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


@router.post("/people/{person_id}/position")
def report_person_position(person_id: str, body: PositionIn, request: Request):
    """A person's phone reports where they are. The privacy rules decide: only inside their shift and on an active route is it stored, otherwise the reason comes back
    (``accepted: false``) and nothing is kept."""
    clock = request.app.state.clock
    with write_db(request) as conn:                                  # no write_txn here: FieldPrivacy creates its tables with executescript, which commits any open transaction
        try:
            return report_position(conn, clock, Ledger(conn, clock), person_id, body.kind, body.lat, body.lng)
        except KeyError:
            raise HTTPException(404, "PERSON_NOT_FOUND")


# ---------------------------------------------------------------------------------------------------- shop hours
@router.get("/shop-hours/{merchant_id}")
def shop_hours(merchant_id: str, conn=Depends(read_db)):
    if conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone() is None:
        raise HTTPException(404, "MERCHANT_NOT_FOUND")
    return _hours_json(get_hours(conn, merchant_id))


class HoursIn(BaseModel):
    open: str
    close: str
    closed_days: list[str] = []


@router.put("/shop-hours/{merchant_id}")
def put_shop_hours(merchant_id: str, body: HoursIn, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        try:
            h = set_hours(conn, clock, merchant_id, body.open, body.close, tuple(body.closed_days), ledger=Ledger(conn, clock))
        except KeyError:
            raise HTTPException(404, "MERCHANT_NOT_FOUND")
        except ValueError as e:
            raise HTTPException(422, {"error": "BAD_HOURS", "reason": str(e)})
        Ledger(conn, clock).append(kind="HUMAN_ACTION", actor="HUMAN:operator", role="ops_admin", payload={"action": "shop_hours", "merchant_id": merchant_id})
    return _hours_json(h)


# ---------------------------------------------------------------------------------------------------- the scam sandbox
class ScamIn(BaseModel):
    merchant_id: str
    amount_paise: int = Field(gt=0)
    reference: Optional[str] = None
    claimed_at: Optional[str] = None


@router.post("/scam-check")
def scam_check(body: ScamIn, request: Request, conn=Depends(read_db)):
    """Read-only: the claim is put into a throwaway copy, never into the real books."""
    try:
        report = check_claim(conn, request.app.state.clock, body.merchant_id, Claim(body.amount_paise, body.reference, body.claimed_at))
    except KeyError:
        raise HTTPException(404, "MERCHANT_NOT_FOUND")
    except ValueError as e:
        raise HTTPException(422, {"error": "BAD_CLAIM", "reason": str(e)})
    return report.as_dict()
