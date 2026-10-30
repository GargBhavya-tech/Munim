"""Mock mandate service (#121; SIMULATED money only): a merchant-set monthly limit and purpose for paying Paytm dues.

``MANDATE-LIMIT-001`` (policy) is the primary guard and runs before the executor: it escalates a payment that would take the month total
over the limit, or whose purpose is not the allowed one. This mock repeats the same checks as a second wall, so a bug upstream still cannot
debit more than the merchant pre-authorised. The limit is CUMULATIVE per calendar month (sim clock), never per payment.

Not in ``app.ROUTERS`` yet (app.py is a shared file); tests mount ``router`` on the app. Debits live in ``mandate_debits``, created lazily
by the first write (``CREATE TABLE IF NOT EXISTS``; the shared schema has only ``mandates``).
"""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, fetch_one, new_id, read_db, require, require_w, token

router = APIRouter(prefix="/mock/mandate")
Conn = Annotated[sqlite3.Connection, Depends(read_db("mandate"))]

ALLOWED_PURPOSE = "paytm_dues"
DDL = ("CREATE TABLE IF NOT EXISTS mandate_debits (debit_id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, merchant_id TEXT NOT NULL, "
       "amount_paise INTEGER NOT NULL, purpose TEXT NOT NULL, month TEXT NOT NULL, receipt_id TEXT NOT NULL, created_at TEXT NOT NULL)")


class MandateIn(BaseModel):
    merchant_id: str
    monthly_limit_paise: int = Field(gt=0)
    purpose: str = ALLOWED_PURPOSE


class PayIn(BaseModel):
    merchant_id: str
    amount_paise: int = Field(gt=0)
    purpose: str = ALLOWED_PURPOSE


def _month(ctx) -> str:
    return ctx.now().strftime("%Y-%m")


def _active(conn, mid: str) -> dict | None:
    return fetch_one(conn, "SELECT * FROM mandates WHERE merchant_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC, mandate_id DESC LIMIT 1", (mid,))


def month_total(conn, mid: str, month: str) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mandate_debits'").fetchone():
        return 0
    return conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM mandate_debits WHERE merchant_id = ? AND month = ?", (mid, month)).fetchone()[0]


def _view(conn, m: dict, month: str) -> dict:
    total = month_total(conn, m["merchant_id"], month)
    return {**m, "month": month, "month_total_paise": total, "remaining_paise": max(0, m["monthly_limit_paise"] - total)}


@router.get("")
def get_mandate(mid: str, conn: Conn, ctx: Ctx):
    """The facts MANDATE-LIMIT-001 needs: mandate_limit_paise, mandate_month_total_paise, mandate_purpose."""
    m = _active(conn, mid)
    if m is None:
        require(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        return {"merchant_id": mid, "mandate": None}
    return {"merchant_id": mid, "mandate": _view(conn, m, _month(ctx))}


@router.get("/receipts")
def receipts(mid: str, conn: Conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mandate_debits'").fetchone():
        return {"merchant_id": mid, "receipts": []}
    return {"merchant_id": mid, "receipts": fetch_all(conn, "SELECT * FROM mandate_debits WHERE merchant_id = ? ORDER BY created_at, debit_id", (mid,))}


@router.post("")
def set_mandate(body: MandateIn, ctx: Ctx, idem: Idem = None):
    """The merchant sets (or replaces) their mandate. Replacing revokes the old one; the month's debits stay counted."""

    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        conn.execute("UPDATE mandates SET status = 'REVOKED' WHERE merchant_id = ? AND status = 'ACTIVE'", (body.merchant_id,))
        mid_ = new_id(conn, "MD")
        conn.execute("INSERT INTO mandates VALUES (?,?,?,?,?,?)", (mid_, body.merchant_id, body.monthly_limit_paise, body.purpose, "ACTIVE", ctx.now_iso()))
        return Result(201, {"mandate_id": mid_, "monthly_limit_paise": body.monthly_limit_paise, "purpose": body.purpose, "status": "ACTIVE"})

    return execute_write(ctx, "mandate", idem, {"op": "mandate.set", "body": body.model_dump()}, fn)


@router.post("/payments")
def pay(body: PayIn, ctx: Ctx, idem: Idem = None):
    """One payment of Paytm dues against the mandate. Needs an active mandate, the allowed purpose, and room in the month; returns a receipt."""

    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        m = _active(conn, body.merchant_id)
        if m is None:
            raise MockError(409, "NO_ACTIVE_MANDATE")
        if body.purpose != m["purpose"] or body.purpose != ALLOWED_PURPOSE:
            raise MockError(409, "PURPOSE_NOT_ALLOWED", body.purpose)
        conn.execute(DDL)
        month = _month(ctx)
        total = month_total(conn, body.merchant_id, month)
        if total + body.amount_paise > m["monthly_limit_paise"]:
            raise MockError(409, "MANDATE_LIMIT_EXCEEDED", f"month total {total} + {body.amount_paise} > {m['monthly_limit_paise']}")
        did = new_id(conn, "MDB")
        receipt = f"RCPT-{token(idem)[:10].upper()}"
        conn.execute("INSERT INTO mandate_debits VALUES (?,?,?,?,?,?,?,?)", (did, m["mandate_id"], body.merchant_id, body.amount_paise, body.purpose, month, receipt, ctx.now_iso()))
        return Result(201, {"debit_id": did, "receipt_id": receipt, "mandate_id": m["mandate_id"], "amount_paise": body.amount_paise, "month": month,
                            "month_total_paise": total + body.amount_paise, "simulated": True})

    return execute_write(ctx, "mandate", idem, {"op": "mandate.pay", "body": body.model_dump()}, fn)
