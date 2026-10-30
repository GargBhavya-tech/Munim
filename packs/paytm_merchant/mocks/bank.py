import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from .common import Ctx, Idem, MockError, Result, execute_write, new_id, read_db, require, require_w, token

router = APIRouter(prefix="/mock/bank")
Conn = Annotated[sqlite3.Connection, Depends(read_db("bank"))]
TRANSIENT_REASONS = {"TIMEOUT"}


@router.get("/{mid}")
def get_bank(mid: str, conn: Conn):
    return require(conn, "SELECT * FROM bank WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")


@router.post("/{mid}/retrigger")
def retrigger(mid: str, ctx: Ctx, idem: Idem = None):
    """Re-run bank verification. Transient failures (timeout) and pending checks verify; mismatches stay failed."""

    def fn(conn):
        b = require_w(conn, "SELECT status, reason, attempts FROM bank WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        if b["status"] == "VERIFIED":
            raise MockError(409, "ALREADY_VERIFIED")
        if b["status"] == "PENDING" or b["reason"] in TRANSIENT_REASONS:
            status, reason = "VERIFIED", None
        else:
            status, reason = "FAILED", b["reason"]
        conn.execute("UPDATE bank SET status=?, reason=?, attempts=attempts+1, updated_at=? WHERE merchant_id=?", (status, reason, ctx.now_iso(), mid))
        return Result(200, {"merchant_id": mid, "status": status, "reason": reason, "attempts": b["attempts"] + 1})

    return execute_write(ctx, "bank", idem, {"op": "bank.retrigger", "mid": mid}, fn)


@router.post("/{mid}/update-link")
def update_link(mid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM bank WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        lid = new_id(conn, "LK")
        url = f"https://pay.paytm.invalid/bank/update/{token(idem)}"
        conn.execute("INSERT INTO links VALUES (?,?,?,?,?,?)", (lid, mid, "BANK_UPDATE", url, ctx.now_iso(), ctx.in_hours(48)))
        return Result(200, {"link_id": lid, "url": url, "expires_at": ctx.in_hours(48)})

    return execute_write(ctx, "bank", idem, {"op": "bank.update_link", "mid": mid}, fn)
