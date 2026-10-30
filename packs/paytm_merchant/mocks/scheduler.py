import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require_w

router = APIRouter(prefix="/mock")
Conn = Annotated[sqlite3.Connection, Depends(read_db("scheduler"))]


class FollowUpIn(BaseModel):
    merchant_id: str
    due_at: str
    kind: str = "follow_up"
    case_id: Optional[str] = None
    note: Optional[str] = None


class BookingIn(BaseModel):
    merchant_id: str
    kind: str
    slot_at: str
    person_id: Optional[str] = None


@router.get("/schedule")
def list_schedule(mid: str, conn: Conn):
    return {
        "merchant_id": mid,
        "follow_ups": fetch_all(conn, "SELECT * FROM schedules WHERE merchant_id = ? ORDER BY due_at", (mid,)),
        "bookings": fetch_all(conn, "SELECT * FROM bookings WHERE merchant_id = ? ORDER BY slot_at", (mid,)),
    }


@router.post("/schedule")
def schedule_follow_up(body: FollowUpIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        sid = new_id(conn, "SCH")
        conn.execute("INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?)", (sid, body.merchant_id, body.kind, body.due_at, body.case_id, body.note, "SCHEDULED", ctx.now_iso()))
        return Result(201, {"schedule_id": sid, "status": "SCHEDULED", "due_at": body.due_at})

    return execute_write(ctx, "scheduler", idem, {"op": "scheduler.follow_up", "body": body.model_dump()}, fn)


@router.delete("/schedule/{sid}")
def cancel_follow_up(sid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        s = require_w(conn, "SELECT * FROM schedules WHERE schedule_id = ?", (sid,), "SCHEDULE_NOT_FOUND")
        if s["status"] == "CANCELLED":
            return Result(200, {"schedule_id": sid, "status": "CANCELLED"})
        conn.execute("UPDATE schedules SET status='CANCELLED' WHERE schedule_id = ?", (sid,))
        return Result(200, {"schedule_id": sid, "status": "CANCELLED"})

    return execute_write(ctx, "scheduler", idem, {"op": "scheduler.cancel_follow_up", "sid": sid}, fn)


@router.post("/bookings")
def create_booking(body: BookingIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        if body.kind not in ("visit", "call"):
            raise MockError(422, "UNKNOWN_BOOKING_KIND")
        bid = new_id(conn, "BK")
        conn.execute("INSERT INTO bookings VALUES (?,?,?,?,?,?,?)", (bid, body.merchant_id, body.kind, body.person_id, body.slot_at, "BOOKED", ctx.now_iso()))
        return Result(201, {"booking_id": bid, "status": "BOOKED", "slot_at": body.slot_at})

    return execute_write(ctx, "scheduler", idem, {"op": "scheduler.booking", "body": body.model_dump()}, fn)


@router.delete("/bookings/{bid}")
def cancel_booking(bid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        b = require_w(conn, "SELECT * FROM bookings WHERE booking_id = ?", (bid,), "BOOKING_NOT_FOUND")
        if b["status"] == "CANCELLED":
            return Result(200, {"booking_id": bid, "status": "CANCELLED"})
        conn.execute("UPDATE bookings SET status='CANCELLED' WHERE booking_id = ?", (bid,))
        return Result(200, {"booking_id": bid, "status": "CANCELLED"})

    return execute_write(ctx, "scheduler", idem, {"op": "scheduler.cancel_booking", "bid": bid}, fn)
