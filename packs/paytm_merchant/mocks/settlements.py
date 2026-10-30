import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .common import Ctx, EventSpec, Idem, MockError, Result, execute_write, fetch_all, read_db, require_w

router = APIRouter(prefix="/mock/settlements")
Conn = Annotated[sqlite3.Connection, Depends(read_db("settlement"))]
EVENT_FOR = {"SETTLED": "SETTLEMENT_SETTLED", "HELD_RISK": "SETTLEMENT_HELD", "FAILED_BANK": "SETTLEMENT_FAILED"}


class AdvanceIn(BaseModel):
    to: str = Field(default="SETTLED", pattern="^(SETTLED|HELD_RISK|FAILED_BANK)$")


@router.get("")
def list_settlements(mid: str, conn: Conn, limit: int = 50):
    return {"merchant_id": mid, "settlements": fetch_all(conn, "SELECT * FROM settlements WHERE merchant_id = ? ORDER BY expected_at DESC LIMIT ?", (mid, limit))}


@router.post("/{sid}/advance")
def advance(sid: str, body: AdvanceIn, ctx: Ctx, idem: Idem = None):
    """Move a settlement forward (demo control, or a human clearing a hold)."""

    def fn(conn):
        s = require_w(conn, "SELECT * FROM settlements WHERE settlement_id = ?", (sid,), "SETTLEMENT_NOT_FOUND")
        if s["status"] == "SETTLED" or (s["status"] != "PENDING" and not (s["status"] == "HELD_RISK" and body.to == "SETTLED")):
            raise MockError(409, "INVALID_TRANSITION", f"{s['status']} -> {body.to}")
        reason = {"HELD_RISK": "RISK_REVIEW", "FAILED_BANK": "BANK_REJECTED"}.get(body.to)
        settled_at = ctx.now_iso() if body.to == "SETTLED" else None
        conn.execute("UPDATE settlements SET status=?, reason=?, settled_at=? WHERE settlement_id=?", (body.to, reason, settled_at, sid))
        ev = EventSpec(EVENT_FOR[body.to], {"settlement_id": sid, "merchant_id": s["merchant_id"], "amount_paise": s["amount_paise"]}, s["merchant_id"])
        return Result(200, {"settlement_id": sid, "status": body.to, "settled_at": settled_at}, [ev])

    return execute_write(ctx, "settlement", idem, {"op": "settlement.advance", "sid": sid, "body": body.model_dump()}, fn)
