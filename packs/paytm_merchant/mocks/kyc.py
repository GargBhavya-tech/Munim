import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from .common import Ctx, Idem, MockError, Result, execute_write, new_id, read_db, require, require_w, token

router = APIRouter(prefix="/mock/kyc")
Conn = Annotated[sqlite3.Connection, Depends(read_db("kyc"))]


@router.get("/{mid}")
def get_kyc(mid: str, conn: Conn):
    return require(conn, "SELECT * FROM kyc WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")


@router.post("/{mid}/resume-link")
def resume_link(mid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        k = require_w(conn, "SELECT status FROM kyc WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
        if k["status"] == "APPROVED":
            raise MockError(409, "ALREADY_APPROVED")
        lid = new_id(conn, "LK")
        url = f"https://pay.paytm.invalid/kyc/resume/{token(idem)}"
        conn.execute("INSERT INTO links VALUES (?,?,?,?,?,?)", (lid, mid, "KYC_RESUME", url, ctx.now_iso(), ctx.in_hours(48)))
        return Result(200, {"link_id": lid, "url": url, "expires_at": ctx.in_hours(48)})

    return execute_write(ctx, "kyc", idem, {"op": "kyc.resume_link", "mid": mid}, fn)
