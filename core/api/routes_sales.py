"""Sales: is the dip real, why, who gets human time, the one save attempt, how a lead is approached, and what a rep has earned (docs/API_CONTRACT.md).

Everything the screens show is computed by the job modules (``jobs/health.py``, ``priority.py``, ``save_plan.py``, ``approach.py``, ``lead_style.py``, ``competitor_sightings.py``,
``field/incentive.py``); this file only reports it. The health job is run on an in-memory COPY of the database (``priority.dry_run_health``), so reading never emits an event or
writes a row. The writes are the small human ones: record a rival-QR sighting, start or close the one save attempt, sign an offer off. An offer is applied through the executor.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import field.briefing as briefing
from core.api.deps import read_db, write_db
from core.audit.ledger import Ledger
from core.memory.store import write_txn
from field.incentive import statement
from field.profiles import load_reps
from jobs import competitor_sightings as cs
from jobs import save_plan as sp
from jobs.approach import LEVEL_WORDS, approach_for
from jobs.briefs import AccountBriefJob, BriefRefused
from jobs.health import ACTION_FOR, DROP_RATIO, PERSIST_DAYS, ZERO_DAYS
from jobs.lead_style import MAX_VOICE_WORDS, plan_style
from jobs.priority import DAYS_PER_MONTH, P_LOSE, P_SAVE, ranked_work, dry_run_health
from jobs.scoring import CAC_PAISE, area_busyness, estimate_turnover

router = APIRouter(prefix="/api/sales")

DIAGNOSIS_TEXT = {
    "DEVICE_OFFLINE": ("A service case", "The Soundbox is offline and nothing has been sold since: this is a repair, not a sales problem"),
    "SETTLEMENT_FAILURE": ("Goes to support", "Settlements are failing and payments are down: a bank or account problem for support to fix"),
    "COMPETITOR_SWITCH": ("A competitor switch: the one we fight for", "Fewer customers for several days, the area is fine, and a rival's QR was seen at the shop"),
    "AREA_WIDE_DIP": ("Munim stays quiet", "The whole pincode dipped (rain, a bandh): nothing is wrong with this merchant"),
    "AREA_OUTAGE": ("Munim stays quiet", "Most devices in the pincode are offline: one area outage, not this merchant's fault"),
    "ZERO_ACTIVITY": ("A check-in call", "Nothing at all for days, and not because the whole area is quiet: closed or moved"),
    "UNEXPLAINED_DROP": ("Retention work, cause unknown", "A real, lasting dip with no device, settlement or area explanation and no rival seen"),
}


def _name(conn, mid: str) -> str | None:
    r = conn.execute("SELECT name FROM merchants WHERE merchant_id = ?", (mid,)).fetchone()
    return r["name"] if r else None


def _monthly(conn, mid: str) -> int:
    r = conn.execute("SELECT avg_daily_volume_paise FROM merchants WHERE merchant_id = ?", (mid,)).fetchone()
    if r is None:
        raise HTTPException(404, "MERCHANT_NOT_FOUND")
    return int(r["avg_daily_volume_paise"] or 0) * DAYS_PER_MONTH


def _finding(conn, clock, mid: str):
    report = dry_run_health(conn, clock)
    return next((f for f in report.findings if f.merchant_id == mid and f.diagnosis in P_LOSE), None)


# ---------------------------------------------------------------------------------------------------- is the dip real, and why
@router.get("/dips")
def dips(request: Request, conn=Depends(read_db)):
    report = dry_run_health(conn, request.app.state.clock)
    rows = []
    for f in report.findings:
        what, why = DIAGNOSIS_TEXT.get(f.diagnosis, (f.diagnosis, ""))
        rows.append({"merchant_id": f.merchant_id, "name": _name(conn, f.merchant_id), "diagnosis": f.diagnosis, "action": f.action, "what": what, "why": why, "detail": f.detail,
                     "quiet": f.action == "NO_ACTION", "retention": f.diagnosis in P_LOSE})
    return {"as_of": report.as_of, "findings": rows, "checked": len(report.scores),
            "rules": {"persist_days": PERSIST_DAYS, "drop_ratio": DROP_RATIO, "zero_days": ZERO_DAYS, "actions": ACTION_FOR}}


# ---------------------------------------------------------------------------------------------------- one list for human time
@router.get("/work")
def work(request: Request, conn=Depends(read_db)):
    clock = request.app.state.clock
    items = ranked_work(conn, clock, dry_run_health(conn, clock))
    return {"items": [i.as_dict() for i in items], "counts": {"merchant_at_risk": sum(i.kind == "merchant_at_risk" for i in items), "new_lead": sum(i.kind == "new_lead" for i in items)},
            "assumptions": {"p_lose": P_LOSE, "p_save": P_SAVE, "cost_paise": CAC_PAISE, "days_per_month": DAYS_PER_MONTH}}


# ---------------------------------------------------------------------------------------------------- the one save attempt
@router.get("/save/{merchant_id}")
def save_view(merchant_id: str, request: Request, conn=Depends(read_db)):
    clock = request.app.state.clock
    monthly = _monthly(conn, merchant_id)
    f = _finding(conn, clock, merchant_id)
    attempt = sp.get_attempt(conn, merchant_id)
    if f is None:
        plan = sp.SavePlan(merchant_id, False, "This merchant has no persistent dip that is a reason to run a save")
    else:
        plan = sp.plan_save(conn, merchant_id, f.diagnosis, f.detail, monthly)
    return {"merchant_id": merchant_id, "name": _name(conn, merchant_id), "diagnosis": f.diagnosis if f else None, "monthly_paise": monthly, "plan": plan.as_dict(), "attempt": attempt}


class RoleIn(BaseModel):
    role: str
    who: str = Field(min_length=1, max_length=40)


def _human(conn, clock, action: str, mid: str, **extra) -> None:
    Ledger(conn, clock).append(kind="HUMAN_ACTION", actor="HUMAN:operator", role="ops_admin", payload={"action": action, "merchant_id": mid, **extra})


@router.post("/save/{merchant_id}/attempt")
def save_attempt(merchant_id: str, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn:
        f = _finding(conn, clock, merchant_id)                            # the health job runs on a copy, so this happens before any write transaction opens
        if f is None:
            raise HTTPException(409, {"error": "NOT_AT_RISK", "reason": "this merchant has no persistent dip that is a reason to run a save"})
        plan = sp.plan_save(conn, merchant_id, f.diagnosis, f.detail, _monthly(conn, merchant_id))
        try:
            with write_txn(conn):
                attempt = sp.start_attempt(conn, clock, Ledger(conn, clock), plan, f.diagnosis)
                _human(conn, clock, "save_attempt", merchant_id, approach=plan.approach)
        except (sp.NotEligible, sp.AlreadyAttempted) as e:
            raise HTTPException(409, {"error": "NO_SAVE_ATTEMPT", "reason": str(e)})
    return {"attempt": attempt, "plan": plan.as_dict()}


@router.post("/save/{merchant_id}/sign-off")
def save_sign_off(merchant_id: str, body: RoleIn, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        try:
            a = sp.sign_off(conn, clock, Ledger(conn, clock), merchant_id, body.role, body.who)
        except sp.NotAllowed as e:
            raise HTTPException(403, {"error": "NOT_ALLOWED", "reason": str(e)})
        except KeyError:
            raise HTTPException(404, "NO_ATTEMPT")
        except ValueError as e:
            raise HTTPException(409, {"error": "CANNOT_SIGN_OFF", "reason": str(e)})
        _human(conn, clock, "save_sign_off", merchant_id, by=f"{body.role}:{body.who}")
    return {"attempt": a}


@router.post("/save/{merchant_id}/offer")
def save_offer(merchant_id: str, request: Request):
    """Apply the approved fee waiver through the executor. Policy can still refuse it, and the refusal comes back as the answer."""
    clock, executor = request.app.state.clock, request.app.state.executor
    with write_db(request) as conn:
        try:
            out = sp.execute_offer(executor, conn, merchant_id)
        except KeyError:
            raise HTTPException(404, "NO_ATTEMPT")
        except ValueError as e:
            raise HTTPException(409, {"error": "CANNOT_APPLY_OFFER", "reason": str(e)})
    return {"status": out.status, "error": out.error, "action_id": out.action_id}


class CloseIn(BaseModel):
    outcome: str = Field(pattern="^(SAVED|LET_GO)$")


@router.post("/save/{merchant_id}/close")
def save_close(merchant_id: str, body: CloseIn, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        try:
            a = sp.close_attempt(conn, clock, Ledger(conn, clock), merchant_id, body.outcome)
        except KeyError:
            raise HTTPException(404, "NO_ATTEMPT")
        except ValueError as e:
            raise HTTPException(409, {"error": "CANNOT_CLOSE", "reason": str(e)})
        _human(conn, clock, "save_close", merchant_id, outcome=body.outcome)
    return {"attempt": a}


# ---------------------------------------------------------------------------------------------------- a rival's QR in a rep's photo
class SightingIn(BaseModel):
    rep_id: str
    merchant_id: str
    photo_ref: str = Field(min_length=1, max_length=200)
    qr_visible: bool
    network: Optional[str] = None
    confidence: float = Field(ge=0, le=1)


@router.post("/sightings")
def sighting_record(body: SightingIn, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        try:
            s = cs.SightingBook(conn, clock, Ledger(conn, clock)).record(body.rep_id, body.merchant_id, body.photo_ref,
                                                                       {"qr_visible": body.qr_visible, "network": body.network, "confidence": body.confidence})
        except KeyError as e:
            raise HTTPException(404, {"error": "NOT_FOUND", "reason": f"{e.args[0]} is not known"})
        except cs.BadReading as e:
            raise HTTPException(422, {"error": "BAD_READING", "reason": str(e)})
    return s.as_dict()


@router.get("/sightings")
def sightings(conn=Depends(read_db)):
    return {"sightings": [s.as_dict() for s in cs.SightingBook.all(_ReadOnlyBook(conn))], "rivals": cs.RIVALS, "min_confidence": cs.MIN_CONFIDENCE}


class _ReadOnlyBook:
    """SightingBook.all needs only a connection; this avoids creating the table on a read-only connection."""

    def __init__(self, conn):
        self.conn = conn

    def _row(self, r):
        return cs.SightingBook._row(self, r)


class ReviewIn(RoleIn):
    accept: bool


@router.post("/sightings/{sighting_id}/review")
def sighting_review(sighting_id: str, body: ReviewIn, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn, write_txn(conn):
        try:
            s = cs.SightingBook(conn, clock, Ledger(conn, clock)).review(sighting_id, body.role, body.who, body.accept)
        except cs.NotAllowed as e:
            raise HTTPException(403, {"error": "NOT_ALLOWED", "reason": str(e)})
        except KeyError:
            raise HTTPException(404, "SIGHTING_NOT_FOUND")
        except ValueError as e:
            raise HTTPException(409, {"error": "CANNOT_REVIEW", "reason": str(e)})
    return s.as_dict()


# ---------------------------------------------------------------------------------------------------- how a lead is approached
@router.get("/style/{lead_id}")
def style(lead_id: str, request: Request):
    clock = request.app.state.clock
    with write_db(request) as conn:                                       # the brief job creates its own table on first use
        row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "LEAD_NOT_FOUND")
        try:
            brief = AccountBriefJob(conn, clock).build(lead_id)
        except BriefRefused as e:
            raise HTTPException(409, {"error": "BRIEF_REFUSED", "reason": str(e)})
        t = estimate_turnover(row["gst_slab"], row["category"], area_busyness(conn, clock).get(row["pincode"]))
        a = approach_for(int(row["score"] or 0), uncertain=t.uncertain if t else True, category=row["category"])
        plan = plan_style(row, brief, a)
    return {"lead_id": lead_id, "name": row["name"], "value": row["score"], "approach": {"level": a.level, "base_level": a.base_level, "bumped": a.bumped, "reasons": list(a.reasons),
                                                                                       "words": LEVEL_WORDS.get(a.level, a.level)},
            "style": plan.as_dict(), "voice_budget_words": MAX_VOICE_WORDS}


# ---------------------------------------------------------------------------------------------------- reps
@router.get("/reps")
def reps(request: Request, conn=Depends(read_db)):
    clock = request.app.state.clock
    out = []
    for r in load_reps(conn):
        out.append({"rep_id": r.rep_id, "name": r.name, "languages": r.languages, "experience_months": r.experience_months, "win_rates": r.win_rates,
                    "statement": statement(conn, clock, r.rep_id).as_dict()})
    return {"reps": out, "briefing": {"new_months": getattr(briefing, "NEW_MONTHS", None), "senior_months": getattr(briefing, "SENIOR_MONTHS", None)}}


# ---------------------------------------------------------------------------------------------------- the sales desk: working a lead
NOTES_SCHEMA = "CREATE TABLE IF NOT EXISTS lead_notes (note_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL, rep_id TEXT, text TEXT NOT NULL, ts TEXT NOT NULL)"
CONTACTABLE = ("NEW", "RESEARCHED", "NURTURE", "NO_REPLY")


def _lifecycle(conn, clock):
    from jobs.lead_lifecycle import LeadLifecycle
    return LeadLifecycle(conn, clock, Ledger(conn, clock))


@router.get("/leads/{lead_id}/desk")
def lead_desk(lead_id: str, conn=Depends(read_db)):
    """Everything a rep needs to work one lead: where it stands, what they may do next, consent and do-not-disturb, and what has happened so far."""
    from jobs.lead_lifecycle import TRANSITIONS
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "LEAD_NOT_FOUND")

    def rows(sql, args):
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        except Exception:                                                  # the table does not exist yet: nothing has happened
            return []
    dnd = bool(row["phone_public"]) and conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (row["phone_public"],)).fetchone() is not None
    consent = [r["channel"] for r in rows("SELECT channel FROM consent WHERE subject_id = ? AND status = 'GRANTED' ORDER BY channel", (lead_id,))]
    state = row["state"]
    return {"lead_id": lead_id, "name": row["name"], "state": state, "matched_merchant_id": row["matched_merchant_id"], "do_not_contact": dnd, "consented_channels": consent,
            "can_send_first_contact": state in CONTACTABLE and row["matched_merchant_id"] is None and not dnd,
            "next_states": sorted(TRANSITIONS.get(state, ())),
            "history": rows("SELECT from_state, to_state, reason, ts FROM lead_transitions WHERE lead_id = ? ORDER BY seq", (lead_id,)),
            "notes": rows("SELECT note_id, rep_id, text, ts FROM lead_notes WHERE lead_id = ? ORDER BY note_id", (lead_id,))}


@router.post("/leads/{lead_id}/contact")
def lead_contact(lead_id: str, request: Request):
    """Send the approved first contact through the executor. Consent, do-not-disturb, quiet hours and the contact budget all still apply; a refusal comes back as the answer."""
    from dataclasses import asdict
    from jobs.outreach import OutreachJob
    clock = request.app.state.clock
    with write_db(request) as conn:
        try:
            res = OutreachJob(conn, clock, Ledger(conn, clock), request.app.state.executor, _lifecycle(conn, clock)).first_contact(lead_id)
        except KeyError:
            raise HTTPException(404, "LEAD_NOT_FOUND")
        _human(conn, clock, "lead_contact", lead_id, status=res.status)
    return asdict(res)


class MoveIn(BaseModel):
    to: str
    reason: Optional[str] = Field(default=None, max_length=120)


@router.post("/leads/{lead_id}/move")
def lead_move(lead_id: str, body: MoveIn, request: Request):
    from jobs.lead_lifecycle import IllegalTransition, NotCleared
    clock = request.app.state.clock
    with write_db(request) as conn:
        try:
            lead = _lifecycle(conn, clock).move(lead_id, body.to, body.reason)
        except KeyError:
            raise HTTPException(404, "LEAD_NOT_FOUND")
        except (IllegalTransition, NotCleared) as e:
            raise HTTPException(409, {"error": "CANNOT_MOVE", "reason": str(e)})
        _human(conn, clock, "lead_move", lead_id, to=body.to)
    return {"lead_id": lead_id, "state": lead.state, "status": lead.status}


class NoteIn(BaseModel):
    rep_id: Optional[str] = None
    text: str = Field(min_length=1, max_length=300)


@router.post("/leads/{lead_id}/note")
def lead_note(lead_id: str, body: NoteIn, request: Request):
    """A note on a lead. Anything that looks like an OTP, PIN, card or account number is redacted before it is stored."""
    from core.memory.redact import redact_or_drop
    clock = request.app.state.clock
    with write_db(request) as conn:
        if conn.execute("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)).fetchone() is None:
            raise HTTPException(404, "LEAD_NOT_FOUND")
        red = redact_or_drop(body.text)
        text = red.text if red else "[unreadable]"
        conn.execute(NOTES_SCHEMA)
        cur = conn.execute("INSERT INTO lead_notes(lead_id, rep_id, text, ts) VALUES (?,?,?,?)", (lead_id, body.rep_id, text, clock.now_iso()))
        _human(conn, clock, "lead_note", lead_id)
    return {"note_id": cur.lastrowid, "text": text}
