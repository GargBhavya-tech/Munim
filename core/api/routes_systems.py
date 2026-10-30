"""The systems panel: a READ-ONLY snapshot of what the mock Paytm systems hold for one merchant (observability, never a decision input)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from core.api.deps import case_json, read_db, rows

router = APIRouter(prefix="/api")


def _one(conn, sql, args):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


@router.get("/merchants/{mid}/systems")
def systems(mid: str, conn=Depends(read_db)):
    m = conn.execute("SELECT * FROM merchants WHERE merchant_id = ?", (mid,)).fetchone()
    if m is None:
        raise HTTPException(404, "MERCHANT_NOT_FOUND")
    open_cases = [case_json(r) for r in conn.execute(
        "SELECT * FROM cases WHERE merchant_id = ? AND state NOT IN ('RESOLVED','DECLINED') ORDER BY opened_at, case_id", (mid,))]
    focus = f"{open_cases[0]['playbook_id']}:{open_cases[0]['step']}" if open_cases else None
    band = _one(conn, "SELECT * FROM offer_bands WHERE segment = ?", (m["segment"],))
    return {
        "kyc": _one(conn, "SELECT * FROM kyc WHERE merchant_id = ?", (mid,)) or {},
        "bank": _one(conn, "SELECT * FROM bank WHERE merchant_id = ?", (mid,)) or {},
        "device": _one(conn, "SELECT * FROM devices WHERE merchant_id = ? ORDER BY device_id LIMIT 1", (mid,)) or {},
        "payments": {"latest": rows(conn.execute("SELECT * FROM payments WHERE merchant_id = ? ORDER BY ts DESC, payment_id DESC LIMIT 5", (mid,))),
                     "settlement": _one(conn, "SELECT * FROM settlements WHERE merchant_id = ? ORDER BY expected_at DESC, settlement_id DESC LIMIT 1", (mid,))},
        "crm": {"open_cases": open_cases, "focus": focus, "language": m["language"]},
        "pricing": {"band": band or {}, "waivers": rows(conn.execute("SELECT * FROM offers WHERE merchant_id = ? ORDER BY applied_at", (mid,)))},
        "scheduler": {"follow_ups": rows(conn.execute("SELECT * FROM schedules WHERE merchant_id = ? ORDER BY due_at", (mid,))),
                      "bookings": rows(conn.execute("SELECT * FROM bookings WHERE merchant_id = ? ORDER BY slot_at", (mid,)))},
        "tickets": rows(conn.execute("SELECT * FROM tickets WHERE merchant_id = ? ORDER BY created_at, ticket_id", (mid,))),
        "comms": rows(conn.execute("SELECT * FROM (SELECT * FROM comms_outbox WHERE merchant_id = ? ORDER BY ts DESC, comm_id DESC LIMIT 10)", (mid,))),
        "handoff_queue": {"open": conn.execute(
            "SELECT COUNT(*) FROM handoffs WHERE status = 'OPEN' AND case_id IN (SELECT case_id FROM cases WHERE merchant_id = ?)", (mid,)).fetchone()[0]},
    }
