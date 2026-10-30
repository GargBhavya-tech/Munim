import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.engine.clock import parse_iso

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require, require_w

router = APIRouter(prefix="/mock/offers")
Conn = Annotated[sqlite3.Connection, Depends(read_db("pricing"))]


class WaiverIn(BaseModel):
    merchant_id: str
    offer_id: str = "FEE_WAIVER_1M"
    months: int = Field(gt=0)


@router.get("/band")
def get_band(mid: str, conn: Conn):
    m = require(conn, "SELECT segment FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    b = require(conn, "SELECT * FROM offer_bands WHERE segment = ?", (m["segment"],), "NO_BAND")
    return {"merchant_id": mid, "segment": m["segment"], "offer_id": b["offer_id"], "max_months": b["max_months"]}


@router.get("")
def list_offers(mid: str, conn: Conn):
    return {"merchant_id": mid, "waivers": fetch_all(conn, "SELECT * FROM offers WHERE merchant_id = ? ORDER BY applied_at", (mid,))}


@router.post("/waiver")
def apply_waiver(body: WaiverIn, ctx: Ctx, idem: Idem = None):
    # The mock does not enforce the band: that is the policy engine's job, and the guard-off twin relies on it.
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        wid = new_id(conn, "W")
        until = ctx.in_hours(24 * 7)
        conn.execute("INSERT INTO offers VALUES (?,?,?,?,?,?,?)", (wid, body.merchant_id, body.offer_id, body.months, "APPLIED", ctx.now_iso(), until))
        return Result(201, {"waiver_id": wid, "status": "APPLIED", "revocable_until": until})

    return execute_write(ctx, "pricing", idem, {"op": "pricing.waiver", "body": body.model_dump()}, fn)


@router.delete("/waiver/{wid}")
def revoke_waiver(wid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        w = require_w(conn, "SELECT * FROM offers WHERE waiver_id = ?", (wid,), "WAIVER_NOT_FOUND")
        if w["status"] == "REVOKED":
            return Result(200, {"waiver_id": wid, "status": "REVOKED"})
        if ctx.now() > parse_iso(w["revocable_until"]):
            raise MockError(409, "WINDOW_CLOSED")
        conn.execute("UPDATE offers SET status='REVOKED' WHERE waiver_id = ?", (wid,))
        return Result(200, {"waiver_id": wid, "status": "REVOKED"})

    return execute_write(ctx, "pricing", idem, {"op": "pricing.revoke", "wid": wid}, fn)
