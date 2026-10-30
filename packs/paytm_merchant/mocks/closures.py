import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.engine.clock import parse_iso

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require_w

router = APIRouter(prefix="/mock/closures")
Conn = Annotated[sqlite3.Connection, Depends(read_db("closures"))]


class ClosureIn(BaseModel):
    merchant_id: str
    reason: Optional[str] = None


@router.get("")
def list_closures(mid: str, conn: Conn):
    return {"merchant_id": mid, "closures": fetch_all(conn, "SELECT * FROM closures WHERE merchant_id = ? ORDER BY created_at, closure_id", (mid,))}


@router.post("")
def create_closure(body: ClosureIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        cid = new_id(conn, "CL")
        until = ctx.in_hours(48)
        conn.execute("INSERT INTO closures VALUES (?,?,?,?,?,?)", (cid, body.merchant_id, body.reason, "REQUESTED", ctx.now_iso(), until))
        return Result(201, {"closure_id": cid, "status": "REQUESTED", "compensable_until": until})

    return execute_write(ctx, "closures", idem, {"op": "closures.create", "body": body.model_dump()}, fn)


@router.delete("/{cid}")
def cancel_closure(cid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        c = require_w(conn, "SELECT * FROM closures WHERE closure_id = ?", (cid,), "CLOSURE_NOT_FOUND")
        if c["status"] == "CANCELLED":
            return Result(200, {"closure_id": cid, "status": "CANCELLED"})
        if c["status"] != "REQUESTED" or ctx.now() > parse_iso(c["compensable_until"]):
            raise MockError(409, "WINDOW_CLOSED", c["status"])
        conn.execute("UPDATE closures SET status='CANCELLED' WHERE closure_id = ?", (cid,))
        return Result(200, {"closure_id": cid, "status": "CANCELLED"})

    return execute_write(ctx, "closures", idem, {"op": "closures.cancel", "cid": cid}, fn)
