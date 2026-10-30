import sqlite3
from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.engine.clock import iso

from .common import Ctx, EventSpec, Idem, Result, execute_write, fetch_all, new_id, read_db, require_w, token

router = APIRouter(prefix="/mock/payments")
Conn = Annotated[sqlite3.Connection, Depends(read_db("payments"))]


class SimulateIn(BaseModel):
    merchant_id: str
    amount_paise: int = Field(gt=0)
    status: str = Field(default="SUCCESS", pattern="^(SUCCESS|FAILED|PENDING)$")
    reference: Optional[str] = None


@router.get("")
def list_payments(mid: str, conn: Conn, limit: int = 100, since: Optional[str] = None):
    sql, args = "SELECT * FROM payments WHERE merchant_id = ?", [mid]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts DESC, payment_id DESC LIMIT ?"
    args.append(limit)
    return {"merchant_id": mid, "payments": fetch_all(conn, sql, tuple(args))}


@router.get("/by-reference")
def by_reference(mid: str, ref: str, conn: Conn):
    """Scam Shield (#86): does this UPI reference exist, and is it THIS merchant's? Another merchant's payment is never disclosed:
    only that the reference exists elsewhere (``belongs_to_merchant`` false, no amount, no time)."""
    row = conn.execute("SELECT * FROM payments WHERE reference = ?", (ref,)).fetchone()
    if row is None:
        return {"merchant_id": mid, "reference": ref, "exists": False, "belongs_to_merchant": False, "payment": None}
    mine = row["merchant_id"] == mid
    return {"merchant_id": mid, "reference": ref, "exists": True, "belongs_to_merchant": mine, "payment": dict(row) if mine else None}


@router.post("/simulate")
def simulate(body: SimulateIn, ctx: Ctx, idem: Idem = None):
    """Demo control: 'customer pays ₹N'. SUCCESS payments join the next day's pending settlement."""

    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        pid = new_id(conn, "P")
        ref = body.reference or f"UPI{int(token(idem, 11), 16) % 10**12:012d}"
        now = ctx.now()
        sid = None
        if body.status == "SUCCESS":
            expected = iso((now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0))
            row = conn.execute(
                "SELECT settlement_id FROM settlements WHERE merchant_id = ? AND status = 'PENDING' AND expected_at = ?",
                (body.merchant_id, expected),
            ).fetchone()
            if row:
                sid = row[0]
                conn.execute("UPDATE settlements SET amount_paise = amount_paise + ? WHERE settlement_id = ?", (body.amount_paise, sid))
            else:
                sid = new_id(conn, "S")
                conn.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?)", (sid, body.merchant_id, body.amount_paise, "PENDING", expected, None, None))
        conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?)", (pid, body.merchant_id, body.amount_paise, body.status, ctx.now_iso(), ref, sid))
        events = []
        if body.status in ("SUCCESS", "FAILED"):
            events.append(EventSpec(f"PAYMENT_{body.status}", {"payment_id": pid, "merchant_id": body.merchant_id, "amount_paise": body.amount_paise}, body.merchant_id))
        return Result(201, {"payment_id": pid, "status": body.status, "settlement_id": sid, "reference": ref}, events)

    return execute_write(ctx, "payments", idem, {"op": "payments.simulate", "body": body.model_dump()}, fn)
