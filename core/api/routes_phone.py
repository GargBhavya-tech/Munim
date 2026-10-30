"""Judge's phone client (#130). NOT registered in core/api/app.py by this ticket; see the report.

POST /api/judge/phone/tokens   {persona_id, ttl_s?}   -> {token, persona_id, expires_at, path}   (the QR encodes ``path``)
POST /api/judge/phone/redeem   {token, persona_id?}   -> the judge view (session, scoreboard, usage, caps); 404/409/410 on refusal

The phone then uses the ordinary /api/judge/sessions/{sid}/* routes and the existing /ws/voice protocol with the session's merchant_id.
The server secret is ``app.state.phone_secret`` (set from config; tests inject it). Without one, issuing is refused (503).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.deps import write_db
from core.api.routes_judge import _view
from core.audit.ledger import Ledger
from core.judge import phone

router = APIRouter(prefix="/api/judge/phone")
STATUS = {"UNKNOWN_TOKEN": 404, "TOKEN_USED": 409, "TOKEN_EXPIRED": 410, "PERSONA_MISMATCH": 403}


class IssueIn(BaseModel):
    persona_id: str
    ttl_s: int = Field(default=phone.DEFAULT_TTL_S, ge=30, le=86_400)


class RedeemIn(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    persona_id: Optional[str] = None


@router.post("/tokens")
def issue(body: IssueIn, request: Request):
    st = request.app.state
    secret = getattr(st, "phone_secret", None)
    if not secret:
        raise HTTPException(503, {"error": "PHONE_SECRET_NOT_CONFIGURED"})
    with st.lock, write_db(request) as conn:
        try:
            t = phone.issue_token(conn, Ledger(conn, st.clock), st.clock, secret=secret, persona_id=body.persona_id, ttl_s=body.ttl_s)
        except KeyError:
            raise HTTPException(404, {"error": "UNKNOWN_PERSONA"})
    return {"token": t["token"], "persona_id": t["persona_id"], "expires_at": t["expires_at"], "path": f"/phone?token={t['token']}", "label": "SIMULATED"}


@router.post("/redeem")
def redeem(body: RedeemIn, request: Request):
    st = request.app.state
    with st.lock, write_db(request) as conn:
        try:
            s = phone.redeem_token(conn, Ledger(conn, st.clock), st.clock, token=body.token, persona_id=body.persona_id)
        except phone.TokenError as e:
            raise HTTPException(STATUS[e.code], {"error": e.code})
        return _view(request, conn, s)
