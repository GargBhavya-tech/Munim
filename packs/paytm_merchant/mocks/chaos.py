from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .common import CHAOS_MODES, SYSTEMS, Ctx, fetch_all

router = APIRouter(prefix="/mock/chaos")


class ChaosIn(BaseModel):
    system: str  # a system name, or '*' for all
    mode: str
    rate: float = Field(ge=0.0, le=1.0)
    param: Optional[float] = None  # latency in ms


@router.get("")
def list_chaos(ctx: Ctx):
    conn = ctx.connect()
    try:
        return {"chaos": fetch_all(conn, "SELECT * FROM chaos ORDER BY system, mode")}
    finally:
        conn.close()


@router.post("")
def set_chaos(body: ChaosIn, ctx: Ctx):
    if body.mode not in CHAOS_MODES:
        raise HTTPException(422, {"error": "UNKNOWN_MODE", "modes": CHAOS_MODES})
    if body.system != "*" and body.system not in SYSTEMS:
        raise HTTPException(422, {"error": "UNKNOWN_SYSTEM", "systems": SYSTEMS})
    conn = ctx.connect()
    try:
        if body.rate == 0:
            conn.execute("DELETE FROM chaos WHERE system = ? AND mode = ?", (body.system, body.mode))
        else:
            conn.execute("INSERT OR REPLACE INTO chaos VALUES (?,?,?,?)", (body.system, body.mode, body.rate, body.param))
        return {"system": body.system, "mode": body.mode, "rate": body.rate}
    finally:
        conn.close()


@router.post("/reset")
def reset_chaos(ctx: Ctx):
    conn = ctx.connect()
    try:
        conn.execute("DELETE FROM chaos")
        return {"reset": True}
    finally:
        conn.close()
