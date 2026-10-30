"""Handoff queue. DB-level handlers over the handoffs table (the integrator may swap in the richer HandoffService)."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from core.api.deps import read_db, write_db
from core.audit.ledger import Ledger
from core.engine import roles as roles_mod
from core.engine.events import append_event
from core.memory.store import write_txn

router = APIRouter(prefix="/api/handoffs")
OPEN_STATUSES = ("OPEN",)


def _json(r) -> dict:
    return {"handoff_id": r["handoff_id"], "case_id": r["case_id"], "packet": json.loads(r["packet_json"]), "status": r["status"],
            "owner_role": r["owner_role"], "deadline": r["deadline"], "created_at": r["created_at"], "verdict": r["verdict"]}


@router.get("")
def list_handoffs(status: Optional[str] = None, conn=Depends(read_db)):
    sql, args = "SELECT * FROM handoffs", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    return {"handoffs": [_json(r) for r in conn.execute(sql + " ORDER BY created_at, handoff_id", args)]}


@router.post("/{handoff_id}/return")
def return_handoff(handoff_id: str, body: dict, request: Request):
    pid, role, instruction = body.get("principal_id"), body.get("role"), body.get("instruction", "")
    if not pid or role not in roles_mod.ROLES or role == "auditor":
        raise HTTPException(403, "ROLE_NOT_ALLOWED")
    st = request.app.state
    with write_db(request) as conn:
        with write_txn(conn):
            h = conn.execute("SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)).fetchone()
            if h is None:
                raise HTTPException(404, "HANDOFF_NOT_FOUND")
            if h["status"] not in OPEN_STATUSES:
                return {"ok": False, "status": h["status"]}
            case = conn.execute("SELECT merchant_id FROM cases WHERE case_id = ?", (h["case_id"],)).fetchone()
            now = st.clock.now_iso()
            conn.execute("UPDATE handoffs SET status = 'RETURNED' WHERE handoff_id = ?", (handoff_id,))
            append_event(conn, event_id=f"HR-{handoff_id}", type="HUMAN_RETURNED", ts_sim=now, case_id=h["case_id"],
                         merchant_id=case["merchant_id"] if case else None, payload={"handoff_id": handoff_id, "instruction": instruction, "by": pid})
            Ledger(conn, st.clock).append(kind="HUMAN_ACTION", actor=f"HUMAN:{pid}", role=role, case_id=h["case_id"],
                                          payload={"action": "return_to_agent", "handoff_id": handoff_id, "instruction": instruction})
    st.after_write()
    return {"ok": True, "status": "RETURNED"}


@router.post("/{handoff_id}/verdict")
def verdict(handoff_id: str, body: dict, request: Request):
    v = body.get("verdict")
    if v not in ("needed", "not_needed"):
        raise HTTPException(422, "verdict must be 'needed' or 'not_needed'")
    with write_db(request) as conn:
        with write_txn(conn):
            h = conn.execute("SELECT case_id FROM handoffs WHERE handoff_id = ?", (handoff_id,)).fetchone()
            if h is None:
                raise HTTPException(404, "HANDOFF_NOT_FOUND")
            conn.execute("UPDATE handoffs SET verdict = ? WHERE handoff_id = ?", (v, handoff_id))
            Ledger(conn, request.app.state.clock).append(kind="HUMAN_ACTION", actor="HUMAN:operator", case_id=h["case_id"],
                                                         payload={"action": "handoff_verdict", "handoff_id": handoff_id, "verdict": v})
    return {"ok": True}
