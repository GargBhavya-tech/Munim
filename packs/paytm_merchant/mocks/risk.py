import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .common import Ctx, Idem, Result, execute_write, fetch_all, new_id, read_db, require, require_w

router = APIRouter(prefix="/mock/risk")
Conn = Annotated[sqlite3.Connection, Depends(read_db("risk"))]


@router.get("/{mid}")
def get_risk(mid: str, conn: Conn):
    require(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    flags = fetch_all(conn, "SELECT * FROM risk_flags WHERE merchant_id = ? ORDER BY ts", (mid,))
    holds = conn.execute("SELECT COUNT(*) FROM settlements WHERE merchant_id = ? AND status = 'HELD_RISK'", (mid,)).fetchone()[0]
    return {"merchant_id": mid, "flags": flags, "open_flags": sum(1 for f in flags if f["status"] == "OPEN"), "settlement_holds": holds, "has_open_hold": holds > 0}


class FlagIn(BaseModel):
    kind: str = Field(pattern="^(FAKE_PAYMENT_SCREENSHOT|FRAUD_CALL|REFERENCE_MISMATCH)$")
    detail: str = Field(default="", max_length=300)


@router.post("/{mid}/flags")
def raise_flag(mid: str, body: FlagIn, ctx: Ctx, idem: Idem = None):
    """Scam Shield: a fraud signal for the risk team (lane P0). Only a reviewer clears it; there is no agent-side undo."""
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        fid = new_id(conn, "F")
        conn.execute("INSERT INTO risk_flags VALUES (?,?,?,?,?,?,?)", (fid, mid, body.kind, "HIGH", body.detail, ctx.now_iso(), "OPEN"))
        return Result(201, {"flag_id": fid, "kind": body.kind, "status": "OPEN"})
    return execute_write(ctx, "risk", idem, {"op": "risk.flag", "mid": mid, "body": body.model_dump()}, fn)
