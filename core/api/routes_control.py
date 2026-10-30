"""Demo controls: kill switch, simulated clock, chaos, and 'customer pays'.

Chaos and 'pay' are SCENARIO controls (the outside world), so they talk to the mock app directly; they are not agent actions
and do not go through the executor. The kill switch is the meta key the executor reads.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.deps import read_db, write_db
from core.audit.ledger import Ledger
from core.memory.store import next_id, write_txn

router = APIRouter(prefix="/api/control")

# Scenario controls (chaos, "customer pays") address the mock world directly. They are demo/test controls, NOT agent actions: they are not
# in the action registry and the agent can never reach them. tests/test_executor.py allow-lists exactly this file for that reason.
CHAOS_PATH = "/mock/chaos"
PAY_PATH = "/mock/payments/simulate"


class KillIn(BaseModel):
    on: bool


class ClockIn(BaseModel):
    advance_hours: float = Field(ge=0)


class ChaosIn(BaseModel):
    system: str
    mode: str
    rate: float = Field(ge=0.0, le=1.0)
    param: Optional[float] = None


class PayIn(BaseModel):
    merchant_id: str
    amount_paise: int = Field(gt=0)


def _kill(conn) -> bool:
    r = conn.execute("SELECT value FROM meta WHERE key = 'kill_switch'").fetchone()
    return bool(r) and r["value"] == "on"


@router.get("")
def control_state(request: Request, conn=Depends(read_db)):
    return {"kill_switch": _kill(conn), "sim_time": request.app.state.clock.now_iso(),
            "chaos": [dict(r) for r in conn.execute("SELECT system, mode, rate, param FROM chaos ORDER BY system, mode")]}


@router.post("/kill-switch")
def kill_switch(body: KillIn, request: Request):
    with write_db(request) as conn, write_txn(conn):
        conn.execute("INSERT INTO meta(key, value) VALUES ('kill_switch', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("on" if body.on else "off",))
        Ledger(conn, request.app.state.clock).append(kind="HUMAN_ACTION", actor="HUMAN:operator", role="ops_admin",
                                                     payload={"action": "kill_switch", "on": body.on})
    return {"kill_switch": body.on}


@router.post("/clock")
def clock(body: ClockIn, request: Request):
    st = request.app.state
    with st.lock:
        st.clock.advance(timedelta(hours=body.advance_hours))
    st.after_write()
    return {"sim_time": st.clock.now_iso()}


@router.post("/chaos")
def chaos(body: ChaosIn, request: Request):
    st = request.app.state
    with st.lock:
        r = st.mock_http.post(CHAOS_PATH, json=body.model_dump())
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.json())
    return {"ok": True}


@router.post("/pay")
def pay(body: PayIn, request: Request):
    st = request.app.state
    with write_db(request) as conn, write_txn(conn):
        key = "demo-pay-" + next_id(conn, "DEMOPAY")
    with st.lock:
        r = st.mock_http.post(PAY_PATH, json={"merchant_id": body.merchant_id, "amount_paise": body.amount_paise},
                              headers={"Idempotency-Key": key})
    if r.status_code != 201:
        raise HTTPException(r.status_code, r.json())
    st.after_write()
    j = r.json()
    return {"payment_id": j["payment_id"], "status": j["status"], "settlement_id": j["settlement_id"]}
