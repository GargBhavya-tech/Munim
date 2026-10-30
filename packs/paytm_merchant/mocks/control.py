"""Scenario controls for demos and tests. NOT agent tools: the action registry (#23) never lists these."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .common import Ctx

router = APIRouter(prefix="/mock/control")


class StatusIn(BaseModel):
    status: str
    reason: Optional[str] = None


def _update(ctx: Ctx, sql: str, args: tuple, what: str):
    conn = ctx.connect()
    try:
        cur = conn.execute(sql, args)
        if cur.rowcount == 0:
            raise HTTPException(404, {"error": f"{what}_NOT_FOUND"})
        return {"ok": True}
    finally:
        conn.close()


@router.post("/kyc/{mid}")
def set_kyc(mid: str, body: StatusIn, ctx: Ctx):
    return _update(ctx, "UPDATE kyc SET status=?, reason=?, updated_at=? WHERE merchant_id=?", (body.status, body.reason, ctx.now_iso(), mid), "MERCHANT")


@router.post("/bank/{mid}")
def set_bank(mid: str, body: StatusIn, ctx: Ctx):
    return _update(ctx, "UPDATE bank SET status=?, reason=?, updated_at=? WHERE merchant_id=?", (body.status, body.reason, ctx.now_iso(), mid), "MERCHANT")


@router.post("/device/{mid}")
def set_device(mid: str, body: StatusIn, ctx: Ctx):
    online = 1 if body.status == "ONLINE" else 0
    return _update(ctx, "UPDATE devices SET status=?, last_heartbeat=?, last_test_announcement_ok=? WHERE merchant_id=?", (body.status, ctx.now_iso(), online, mid), "DEVICE")


@router.post("/replacement/{rid}")
def set_replacement(rid: str, body: StatusIn, ctx: Ctx):
    return _update(ctx, "UPDATE device_replacements SET status=? WHERE replacement_id=?", (body.status, rid), "REPLACEMENT")
