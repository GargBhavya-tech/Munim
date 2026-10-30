"""Mock lender systems: a loan account, promises-to-pay and a borrower inbox. SIMULATED. Writes honour Idempotency-Key: the same key returns
the stored response and changes nothing. Scenario controls (the borrower pays, a dispute is lodged) live under /mock/lender/control and
are the outside world, never an agent action."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.engine.clock import SimClock
from core.memory.store import connect, write_txn
from packs.demo_lender.store import ensure_schema, next_id


class PromiseIn(BaseModel):
    promised_on: str
    borrower_consented: bool = False


class MessageIn(BaseModel):
    channel: str
    body: str = Field(min_length=1, max_length=2000)
    language: Optional[str] = None
    case_id: Optional[str] = None
    recipient: str = "borrower"


class FlagIn(BaseModel):
    value: bool = True


def create_app(db_path, clock: SimClock) -> FastAPI:
    app = FastAPI(title="SIMULATED lender systems")

    def db() -> sqlite3.Connection:
        c = connect(db_path)
        ensure_schema(c)
        return c

    def idem_write(key: str | None, fn):
        c = db()
        try:
            with write_txn(c):
                if key:
                    hit = c.execute("SELECT status, body FROM lender_idem WHERE key = ?", (key,)).fetchone()
                    if hit:
                        return JSONResponse(json.loads(hit["body"]), status_code=hit["status"])
                status, body = fn(c)
                if key:
                    c.execute("INSERT INTO lender_idem VALUES (?,?,?)", (key, status, json.dumps(body)))
            return JSONResponse(body, status_code=status)
        finally:
            c.close()

    def loan_of(c: sqlite3.Connection, bid: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM loans WHERE borrower_id = ? ORDER BY loan_id DESC", (bid,)).fetchone()
        if row is None:
            raise HTTPException(404, {"error": "BORROWER_NOT_FOUND"})
        return row

    def today() -> date:
        return clock.now().date()

    @app.get("/mock/lender/{bid}/loan")
    def loan(bid: str):
        c = db()
        try:
            ln = loan_of(c, bid)
            due = date.fromisoformat(ln["due_date"])
            dpd = max((today() - due).days, 0) if ln["status"] == "OVERDUE" else 0
            p = c.execute("SELECT * FROM promises WHERE loan_id = ? AND status != 'CANCELLED' ORDER BY created_at DESC, promise_id DESC", (ln["loan_id"],)).fetchone()
            ptp = "NONE"
            if p is not None:
                ptp = "KEPT" if p["status"] == "KEPT" else ("BROKEN" if date.fromisoformat(p["promised_on"]) < today() else "ACTIVE")
            return {"borrower_id": bid, "loan_id": ln["loan_id"], "status": ln["status"], "due_amount_paise": ln["due_amount_paise"],
                    "due_date": ln["due_date"], "days_past_due": dpd, "in_dispute": bool(ln["in_dispute"]), "ptp_status": ptp,
                    "promise_id": p["promise_id"] if p else None, "promised_on": p["promised_on"] if p else None}
        finally:
            c.close()

    @app.post("/mock/lender/{bid}/promises", status_code=201)
    def record_promise(bid: str, body: PromiseIn, idempotency_key: Optional[str] = Header(default=None)):
        def fn(c):
            ln = loan_of(c, bid)
            live = c.execute("SELECT promise_id, promised_on FROM promises WHERE loan_id = ? AND status = 'ACTIVE'", (ln["loan_id"],)).fetchone()
            if live:
                return 200, {"promise_id": live[0], "status": "ACTIVE", "existing": True}
            pid = next_id(c, "PTP")
            # the amount is the system's, never the caller's
            c.execute("INSERT INTO promises VALUES (?,?,?,?,?,?)", (pid, ln["loan_id"], body.promised_on, ln["due_amount_paise"], "ACTIVE", clock.now_iso()))
            return 201, {"promise_id": pid, "status": "ACTIVE", "amount_paise": ln["due_amount_paise"], "promised_on": body.promised_on}
        return idem_write(idempotency_key, fn)

    @app.delete("/mock/lender/{bid}/promises/{pid}")
    def cancel_promise(bid: str, pid: str, idempotency_key: Optional[str] = Header(default=None)):
        def fn(c):
            c.execute("UPDATE promises SET status = 'CANCELLED' WHERE promise_id = ? AND status = 'ACTIVE'", (pid,))
            return 200, {"promise_id": pid, "status": "CANCELLED"}
        return idem_write(idempotency_key, fn)

    @app.post("/mock/lender/{bid}/messages", status_code=201)
    def message(bid: str, body: MessageIn, idempotency_key: Optional[str] = Header(default=None)):
        def fn(c):
            loan_of(c, bid)
            mid = next_id(c, "LM")
            c.execute("INSERT INTO lender_messages VALUES (?,?,?,?,?)", (mid, bid, body.channel, body.body, clock.now_iso()))
            return 201, {"message_id": mid, "status": "SENT"}
        return idem_write(idempotency_key, fn)

    # -- scenario controls ---------------------------------------------------------------------------------------------------
    @app.post("/mock/lender/control/pay/{bid}")
    def pay(bid: str):
        c = db()
        try:
            ln = loan_of(c, bid)
            c.execute("UPDATE loans SET status = 'PAID' WHERE loan_id = ?", (ln["loan_id"],))
            c.execute("UPDATE promises SET status = 'KEPT' WHERE loan_id = ? AND status = 'ACTIVE'", (ln["loan_id"],))
            return {"ok": True}
        finally:
            c.close()

    @app.post("/mock/lender/control/dispute/{bid}")
    def dispute(bid: str, body: FlagIn):
        c = db()
        try:
            c.execute("UPDATE loans SET in_dispute = ? WHERE borrower_id = ?", (int(body.value), bid))
            return {"ok": True}
        finally:
            c.close()

    return app
