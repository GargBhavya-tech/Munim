"""Plain-language policy compiler API (#134). NOT registered in core/api/app.py by this ticket; see the report for the registration lines.

POST /api/policy/compile   {text}                                          -> draft {dsl, rule_id, rule_hash, summary, diff, dry_run, ok, errors}   422 {error, message}
POST /api/policy/approve   {dsl, rule_hash, principal_id, role, text?}     -> {rule_id, rule_hash, ledger_seq, policy_hash, policy_hash_before}      403 NOT_OPS_ADMIN, 409, 422
POST /api/policy/revert    {rule_id, principal_id, role}                   -> {rule_id, ledger_seq, policy_hash, policy_hash_before}
GET  /api/policy/rules                                                     -> {policy_hash, pack_rules[], compiled[]}

The engine is the live one (app.state.executor.policy), so an approved rule takes effect for the next action. A phraser (LLM path) may be set at
app.state.policy_phraser; without one only the templates work. Role is checked on the server in core.judge.policy_compiler, never trusted from the UI.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.actions.types import Principal
from core.api.deps import write_db
from core.audit.ledger import Ledger
from core.judge import policy_compiler as pc

router = APIRouter(prefix="/api/policy")
STATUS = {"NOT_OPS_ADMIN": 403, "ALREADY_ACTIVE": 409, "HASH_MISMATCH": 409, "NOT_ACTIVE": 404}


class CompileIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ApproveIn(BaseModel):
    dsl: dict
    rule_hash: str
    principal_id: str
    role: Optional[str] = None
    text: str = Field(default="", max_length=500)


class RevertIn(BaseModel):
    rule_id: str
    principal_id: str
    role: Optional[str] = None


def _engine(request: Request, conn):
    eng = request.app.state.executor.policy
    pc.sync_engine(eng, conn)                       # a fresh process picks up rules approved earlier
    return eng


def _probes(request: Request, conn) -> list[pc.Probe]:
    st = request.app.state
    return pc.probes_from_ledger(conn) + pc.probes_from_frozen(st.root / "eval" / "frozen" / "probes" / "policy_probes.json")


def _err(e: pc.PolicyCompileError) -> JSONResponse:
    return JSONResponse(e.as_json(), status_code=STATUS.get(e.code, 422))


@router.post("/compile")
def compile_rule(body: CompileIn, request: Request):
    st = request.app.state
    with st.lock, write_db(request) as conn:
        try:
            eng = _engine(request, conn)
            return pc.compile_text(body.text, eng, _probes(request, conn), phraser=getattr(st, "policy_phraser", None)).as_json()
        except pc.PolicyCompileError as e:
            return _err(e)


@router.post("/approve")
def approve_rule(body: ApproveIn, request: Request):
    st = request.app.state
    with st.lock, write_db(request) as conn:
        try:
            eng = _engine(request, conn)
            ledger = Ledger(conn, st.clock, policy_hash=lambda: eng.policy_hash)
            return pc.approve(eng, ledger, body.dsl, Principal("HUMAN", body.principal_id, body.role), expected_hash=body.rule_hash,
                              probes=_probes(request, conn), text=body.text)
        except pc.PolicyCompileError as e:
            return _err(e)


@router.post("/revert")
def revert_rule(body: RevertIn, request: Request):
    st = request.app.state
    with st.lock, write_db(request) as conn:
        try:
            eng = _engine(request, conn)
            ledger = Ledger(conn, st.clock, policy_hash=lambda: eng.policy_hash)
            return pc.revert(eng, ledger, body.rule_id, Principal("HUMAN", body.principal_id, body.role))
        except pc.PolicyCompileError as e:
            return _err(e)


@router.get("/rules")
def rules(request: Request):
    with write_db(request) as conn:
        eng = _engine(request, conn)
        return {"policy_hash": eng.policy_hash, "label": "SIMULATED",
                "pack_rules": [{"rule_id": r.id, "title": r.title, "severity": r.severity} for r in pc.base_rules(eng)],
                "compiled": pc.active_compiled(conn)}


# -- guided tour of Judge Mode (#135) ---------------------------------------------------------------------
from fastapi import HTTPException                     # noqa: E402

from core.judge import session as sess                # noqa: E402
from core.judge import tour as tour_mod               # noqa: E402

tour_router = APIRouter(prefix="/api/judge/tour")


def _clips(request: Request, steps: list[dict]) -> list[dict]:
    pub = request.app.state.root / "dashboard" / "public"
    gone = set(tour_mod.missing_clips(pub, steps))
    return [{**s, "clip_available": s["id"] not in gone} for s in steps]


@tour_router.get("")
def tour_steps(request: Request):
    """GET /api/judge/tour -> {label, steps[{id,title,ui_element,ui_label?,instruction,fact,proves,clip,clip_available}]}. Static data from tour.yaml."""
    return {"label": "SIMULATED", "steps": _clips(request, tour_mod.load_tour())}


@tour_router.get("/{sid}")
def tour_progress(sid: str, request: Request):
    """GET /api/judge/tour/{sid} -> {steps[{id,done,metric,value,op,needed,evidence_seq}], done, total, next}. Computed from the ledger on every call."""
    with write_db(request) as conn:
        try:
            s = sess.load_session(conn, sid)
        except sess.UnknownSession:
            raise HTTPException(404, {"error": "UNKNOWN_SESSION"})
        return tour_mod.progress(conn, s)
