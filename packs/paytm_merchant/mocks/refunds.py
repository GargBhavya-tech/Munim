import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require_w

router = APIRouter(prefix="/mock/refunds")
Conn = Annotated[sqlite3.Connection, Depends(read_db("refunds"))]


class RefundIn(BaseModel):
    merchant_id: str
    payment_id: str
    amount_paise: int = Field(gt=0)


@router.get("")
def list_refunds(mid: str, conn: Conn):
    return {"merchant_id": mid, "refunds": fetch_all(conn, "SELECT * FROM refunds WHERE merchant_id = ? ORDER BY created_at, refund_id", (mid,))}


@router.post("")
def create_refund(body: RefundIn, ctx: Ctx, idem: Idem = None):
    """A refund request waits for the merchant to confirm in their own app; a chat 'yes' is not confirmation."""

    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        p = require_w(conn, "SELECT * FROM payments WHERE payment_id = ? AND merchant_id = ?", (body.payment_id, body.merchant_id), "PAYMENT_NOT_FOUND")
        if p["status"] != "SUCCESS":
            raise MockError(409, "PAYMENT_NOT_REFUNDABLE", p["status"])
        if body.amount_paise > p["amount_paise"]:
            raise MockError(422, "REFUND_EXCEEDS_PAYMENT")
        rid = new_id(conn, "RF")
        until = ctx.in_hours(24)
        conn.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?,?)", (rid, body.merchant_id, body.payment_id, body.amount_paise, "AWAITING_MERCHANT_CONFIRM", ctx.now_iso(), until))
        return Result(201, {"refund_id": rid, "status": "AWAITING_MERCHANT_CONFIRM", "compensable_until": until})

    return execute_write(ctx, "refunds", idem, {"op": "refunds.create", "body": body.model_dump()}, fn)


@router.post("/{rid}/merchant-confirm")
def merchant_confirm(rid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        r = require_w(conn, "SELECT * FROM refunds WHERE refund_id = ?", (rid,), "REFUND_NOT_FOUND")
        if r["status"] == "PROCESSED":
            return Result(200, {"refund_id": rid, "status": "PROCESSED"})
        if r["status"] != "AWAITING_MERCHANT_CONFIRM":
            raise MockError(409, "INVALID_TRANSITION", r["status"])
        conn.execute("UPDATE refunds SET status='PROCESSED' WHERE refund_id = ?", (rid,))
        return Result(200, {"refund_id": rid, "status": "PROCESSED"})

    return execute_write(ctx, "refunds", idem, {"op": "refunds.confirm", "rid": rid}, fn)


@router.delete("/{rid}")
def cancel_refund(rid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        r = require_w(conn, "SELECT * FROM refunds WHERE refund_id = ?", (rid,), "REFUND_NOT_FOUND")
        if r["status"] == "CANCELLED":
            return Result(200, {"refund_id": rid, "status": "CANCELLED"})
        if r["status"] != "AWAITING_MERCHANT_CONFIRM":
            raise MockError(409, "WINDOW_CLOSED", r["status"])
        conn.execute("UPDATE refunds SET status='CANCELLED' WHERE refund_id = ?", (rid,))
        return Result(200, {"refund_id": rid, "status": "CANCELLED"})

    return execute_write(ctx, "refunds", idem, {"op": "refunds.cancel", "rid": rid}, fn)
