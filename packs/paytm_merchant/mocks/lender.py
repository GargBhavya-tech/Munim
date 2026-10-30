import sqlite3
from datetime import timedelta
from typing import Annotated

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.engine.clock import iso

from .common import Ctx, Idem, MockError, Result, execute_write, new_id, read_db, require, require_w, token

router = APIRouter(prefix="/mock/lender")
Conn = Annotated[sqlite3.Connection, Depends(read_db("lender"))]

PARTNER = "Demo Partner NBFC"
MIN_30D_VOLUME_PAISE = 10_000_000  # ₹1 lakh over 30 days (synthetic rule)
STEP_PAISE = 10_000_000            # offers in ₹1 lakh steps
CAP_PAISE = 250_000_000            # ₹25 lakh
TERMS_REF = "kb:loan_partner_terms#indicative_terms@v1"
KFS_NOTE = "The lender will show you a Key Fact Statement with all charges before you agree."


class LinkIn(BaseModel):
    merchant_id: str


def eligibility(conn: sqlite3.Connection, mid: str, now) -> dict:
    """Deterministic rules over synthetic data. The partner decides; Munim only relays this result."""
    cutoff = iso(now - timedelta(days=30))
    kyc = conn.execute("SELECT status FROM kyc WHERE merchant_id = ?", (mid,)).fetchone()
    bank = conn.execute("SELECT status FROM bank WHERE merchant_id = ?", (mid,)).fetchone()
    has_device = conn.execute("SELECT COUNT(*) FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0] > 0
    held = conn.execute("SELECT COUNT(*) FROM settlements WHERE merchant_id = ? AND status = 'HELD_RISK'", (mid,)).fetchone()[0] > 0
    vol = conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE merchant_id = ? AND status = 'SUCCESS' AND ts >= ?", (mid, cutoff)).fetchone()[0]
    reasons = []
    if not kyc or kyc["status"] != "APPROVED":
        reasons.append("KYC_NOT_APPROVED")
    if not bank or bank["status"] != "VERIFIED":
        reasons.append("BANK_NOT_VERIFIED")
    if not has_device:
        reasons.append("NO_DEVICE")
    if held:
        reasons.append("SETTLEMENT_ON_HOLD")
    if vol < MIN_30D_VOLUME_PAISE:
        reasons.append("INSUFFICIENT_VOLUME")
    eligible = not reasons
    max_amount = min(CAP_PAISE, (vol * 2 // STEP_PAISE) * STEP_PAISE) if eligible else 0
    return {"merchant_id": mid, "eligible": eligible, "max_amount_paise": max_amount, "indicative_terms_ref": TERMS_REF if eligible else None,
            "partner": PARTNER, "reasons": reasons}


@router.get("/eligibility")
def get_eligibility(mid: str, conn: Conn, ctx: Ctx):
    require(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    return eligibility(conn, mid, ctx.now())


@router.get("/applications")
def list_applications(mid: str, conn: Conn):
    from .common import fetch_all
    return {"merchant_id": mid, "applications": fetch_all(conn, "SELECT * FROM lender_apps WHERE merchant_id = ? ORDER BY created_at, app_id", (mid,))}


@router.post("/application-link")
def application_link(body: LinkIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        if not eligibility(conn, body.merchant_id, ctx.now())["eligible"]:
            raise MockError(409, "NOT_ELIGIBLE")  # the lender decides; we never override it
        aid = new_id(conn, "LA")
        link = f"https://partner-lender.invalid/apply/{token(idem)}"
        conn.execute("INSERT INTO lender_apps VALUES (?,?,?,?,?,?)", (aid, body.merchant_id, PARTNER, "LINK_SENT", link, ctx.now_iso()))
        return Result(201, {"app_id": aid, "link": link, "partner": PARTNER, "kfs_note": KFS_NOTE})

    return execute_write(ctx, "lender", idem, {"op": "lender.link", "body": body.model_dump()}, fn)


# ---------------------------------------------------------------------------------------------- the lender's own agent (#115)
# Agent to agent: Munim's client (integrations/lender_agent.py) sends a SIGNED, consent-scoped, token-only request through the executor. This route is
# the lender's side: it checks the signature, the scope and the age, applies the LENDER's eligibility rule and answers. Munim never decides eligibility.
SIMULATED_SIGNING_KEY = b"simulated-demo-lender-signing"     # SIMULATED shared HMAC key between two mocks; not a credential


class AgentAsk(BaseModel):
    merchant_token: str
    consent_scope: str
    purpose: str
    ts: str
    signature: str


@router.get("/agent")
def lender_agent(conn: Conn, ctx: Ctx, body: Annotated[AgentAsk, Query()]):
    from integrations.lender_agent import MAX_AGE, PURPOSES, SCOPE, merchant_token, sign
    from core.engine.clock import parse_iso
    req = body.model_dump(exclude={"signature"})
    if not hmac.compare_digest(sign(SIMULATED_SIGNING_KEY, req), body.signature):
        raise HTTPException(401, {"error": "BAD_SIGNATURE"})
    if body.consent_scope != SCOPE or body.purpose not in PURPOSES:
        raise HTTPException(403, {"error": "SCOPE_NOT_ACCEPTED"})
    if ctx.now() - parse_iso(body.ts) > MAX_AGE:
        raise HTTPException(409, {"error": "STALE_REQUEST"})
    mid = next((r[0] for r in conn.execute("SELECT merchant_id FROM merchants")
                if hmac.compare_digest(merchant_token(SIMULATED_SIGNING_KEY, r[0]), body.merchant_token)), None)
    if mid is None:
        raise HTTPException(404, {"error": "UNKNOWN_TOKEN"})
    e = eligibility(conn, mid, ctx.now())
    kfs = "KFS-" + hashlib.sha256(f"{mid}|{body.ts}".encode()).hexdigest()[:10].upper()
    return {"partner": PARTNER, "eligible": e["eligible"], "max_amount_paise": e["max_amount_paise"], "reasons": e["reasons"],
            "kfs_ref": kfs if e["eligible"] else None}
