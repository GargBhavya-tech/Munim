import sqlite3
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.engine.clock import iso, parse_iso

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, fetch_one, new_id, read_db, require, require_w

router = APIRouter(prefix="/mock/devices")
Conn = Annotated[sqlite3.Connection, Depends(read_db("device"))]


class ReplacementIn(BaseModel):
    reason: str = "UNSPECIFIED"


class OrderIn(BaseModel):
    model: str = "Soundbox 4G"


@router.get("/{mid}")
def get_device(mid: str, conn: Conn, ctx: Ctx):
    require(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    d = fetch_one(conn, "SELECT * FROM devices WHERE merchant_id = ?", (mid,))
    if d is None:
        orders = fetch_all(conn, "SELECT * FROM device_orders WHERE merchant_id = ? ORDER BY created_at", (mid,))   # a QR-only merchant can have an order
        return {"merchant_id": mid, "device_id": None, "status": "NONE", "replacements_last_30d": 0, "replacements": [], "orders": orders}
    reps = fetch_all(conn, "SELECT * FROM device_replacements WHERE merchant_id = ? ORDER BY created_at", (mid,))
    cutoff = iso(ctx.now() - timedelta(days=30))
    d["replacements"] = reps
    d["orders"] = fetch_all(conn, "SELECT * FROM device_orders WHERE merchant_id = ? ORDER BY created_at", (mid,))
    d["replacements_last_30d"] = sum(1 for r in reps if r["status"] != "CANCELLED" and r["created_at"] >= cutoff)
    from .remote import remote_attempts
    d["remote_fix_attempts"] = remote_attempts(conn, mid)
    return d


@router.post("/{mid}/replacement")
def create_replacement(mid: str, body: ReplacementIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        d = fetch_one(conn, "SELECT device_id FROM devices WHERE merchant_id = ?", (mid,))
        if d is None:
            raise MockError(409, "NO_DEVICE")
        rid = new_id(conn, "R")
        until = ctx.in_hours(24)
        conn.execute("INSERT INTO device_replacements VALUES (?,?,?,?,?,?,?)", (rid, d["device_id"], mid, body.reason, "CREATED", ctx.now_iso(), until))
        return Result(201, {"replacement_id": rid, "device_id": d["device_id"], "status": "CREATED", "compensable_until": until})

    return execute_write(ctx, "device", idem, {"op": "device.replacement", "mid": mid, "body": body.model_dump()}, fn)


@router.delete("/{mid}/replacement/{rid}")
def cancel_replacement(mid: str, rid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        r = require_w(conn, "SELECT * FROM device_replacements WHERE replacement_id = ? AND merchant_id = ?", (rid, mid), "REPLACEMENT_NOT_FOUND")
        if r["status"] == "CANCELLED":
            return Result(200, {"replacement_id": rid, "status": "CANCELLED"})
        if r["status"] != "CREATED" or ctx.now() > parse_iso(r["compensable_until"]):
            raise MockError(409, "WINDOW_CLOSED", f"status={r['status']}")
        conn.execute("UPDATE device_replacements SET status='CANCELLED' WHERE replacement_id = ?", (rid,))
        return Result(200, {"replacement_id": rid, "status": "CANCELLED"})

    return execute_write(ctx, "device", idem, {"op": "device.cancel_replacement", "mid": mid, "rid": rid}, fn)


@router.post("/{mid}/order")
def create_order(mid: str, body: OrderIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        oid = new_id(conn, "O")
        until = ctx.in_hours(24)
        conn.execute("INSERT INTO device_orders VALUES (?,?,?,?,?,?)", (oid, mid, body.model, "PLACED", ctx.now_iso(), until))
        return Result(201, {"order_id": oid, "status": "PLACED", "compensable_until": until})

    return execute_write(ctx, "device", idem, {"op": "device.order", "mid": mid, "body": body.model_dump()}, fn)


@router.delete("/{mid}/order/{oid}")
def cancel_order(mid: str, oid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        o = require_w(conn, "SELECT * FROM device_orders WHERE order_id = ? AND merchant_id = ?", (oid, mid), "ORDER_NOT_FOUND")
        if o["status"] == "CANCELLED":
            return Result(200, {"order_id": oid, "status": "CANCELLED"})
        if o["status"] != "PLACED" or ctx.now() > parse_iso(o["compensable_until"]):
            raise MockError(409, "WINDOW_CLOSED", f"status={o['status']}")
        conn.execute("UPDATE device_orders SET status='CANCELLED' WHERE order_id = ?", (oid,))
        return Result(200, {"order_id": oid, "status": "CANCELLED"})

    return execute_write(ctx, "device", idem, {"op": "device.cancel_order", "mid": mid, "oid": oid}, fn)
