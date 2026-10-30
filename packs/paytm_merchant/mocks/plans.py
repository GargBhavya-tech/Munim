import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.engine.clock import IST, iso, parse_iso

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require, require_w

router = APIRouter(prefix="/mock/plans")
Conn = Annotated[sqlite3.Connection, Depends(read_db("plans"))]
CATALOG = {"BASIC": 0, "SOUNDBOX_STD": 14900, "SOUNDBOX_PLUS": 24900}


class ChangeIn(BaseModel):
    to_plan_id: str


@router.get("/{mid}")
def get_plan(mid: str, conn: Conn):
    p = require(conn, "SELECT * FROM plans WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    p["changes"] = fetch_all(conn, "SELECT * FROM plan_changes WHERE merchant_id = ? ORDER BY created_at", (mid,))
    p["catalog"] = CATALOG
    return p


@router.post("/{mid}")
def change_plan(mid: str, body: ChangeIn, ctx: Ctx, idem: Idem = None):
    """Plan change takes effect at the billing date and can be reverted until then."""

    def fn(conn):
        p = require_w(conn, "SELECT * FROM plans WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        if body.to_plan_id not in CATALOG:
            raise MockError(422, "UNKNOWN_PLAN")
        if body.to_plan_id == p["plan_id"]:
            raise MockError(409, "SAME_PLAN")
        cid = new_id(conn, "PC")
        until = iso(datetime.fromisoformat(p["billing_date"]).replace(tzinfo=IST))
        conn.execute("INSERT INTO plan_changes VALUES (?,?,?,?,?,?,?)", (cid, mid, p["plan_id"], body.to_plan_id, "SCHEDULED", ctx.now_iso(), until))
        conn.execute("UPDATE plans SET pending_plan_id = ? WHERE merchant_id = ?", (body.to_plan_id, mid))
        return Result(201, {"change_id": cid, "status": "SCHEDULED", "effective_on": p["billing_date"], "revertible_until": until})

    return execute_write(ctx, "plans", idem, {"op": "plans.change", "mid": mid, "body": body.model_dump()}, fn)


@router.delete("/{mid}/change/{cid}")
def revert_change(mid: str, cid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        c = require_w(conn, "SELECT * FROM plan_changes WHERE change_id = ? AND merchant_id = ?", (cid, mid), "CHANGE_NOT_FOUND")
        if c["status"] == "REVERTED":
            return Result(200, {"change_id": cid, "status": "REVERTED"})
        if ctx.now() > parse_iso(c["revertible_until"]):
            raise MockError(409, "WINDOW_CLOSED")
        conn.execute("UPDATE plan_changes SET status='REVERTED' WHERE change_id = ?", (cid,))
        conn.execute("UPDATE plans SET pending_plan_id = NULL WHERE merchant_id = ?", (mid,))
        return Result(200, {"change_id": cid, "status": "REVERTED"})

    return execute_write(ctx, "plans", idem, {"op": "plans.revert", "mid": mid, "cid": cid}, fn)
