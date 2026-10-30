"""Mock consumer systems (#89): UPI transactions, disputes, refunds and an app inbox. SIMULATED. Writes honour Idempotency-Key: the same
key returns the stored response and changes nothing. Scenario controls (reverse a debit, credit a refund) live under /mock/consumer/control."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.engine.clock import SimClock
from core.memory.store import connect, write_txn
from packs.paytm_consumer.store import ensure_schema, next_id


class DisputeIn(BaseModel):
    txn_id: str


class MessageIn(BaseModel):
    channel: str
    body: str = Field(min_length=1, max_length=2000)
    language: Optional[str] = None
    case_id: Optional[str] = None


class StatusIn(BaseModel):
    status: str


def create_app(db_path, clock: SimClock) -> FastAPI:
    app = FastAPI(title="SIMULATED consumer systems")

    def db() -> sqlite3.Connection:
        c = connect(db_path)
        ensure_schema(c)
        return c

    def rows(sql: str, args: tuple) -> list[dict]:
        c = db()
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def idem_write(key: str | None, fn):
        c = db()
        try:
            with write_txn(c):
                if key:
                    hit = c.execute("SELECT status, body FROM consumer_idem WHERE key = ?", (key,)).fetchone()
                    if hit:
                        return JSONResponse(json.loads(hit["body"]), status_code=hit["status"])
                status, body = fn(c)
                if key:
                    c.execute("INSERT INTO consumer_idem VALUES (?,?,?)", (key, status, json.dumps(body)))
            return JSONResponse(body, status_code=status)
        finally:
            c.close()

    def known(c: sqlite3.Connection, cid: str) -> None:
        if c.execute("SELECT 1 FROM consumers WHERE consumer_id = ?", (cid,)).fetchone() is None:
            raise HTTPException(404, {"error": "CONSUMER_NOT_FOUND"})

    @app.get("/mock/consumer/{cid}/transactions")
    def transactions(cid: str):
        return {"consumer_id": cid, "transactions": rows("SELECT * FROM consumer_txns WHERE consumer_id = ? ORDER BY ts DESC, txn_id DESC", (cid,)),
                "disputes": rows("SELECT * FROM consumer_disputes WHERE consumer_id = ? ORDER BY created_at DESC, dispute_id DESC", (cid,))}

    @app.get("/mock/consumer/{cid}/refunds")
    def refunds(cid: str):
        out = rows("SELECT * FROM consumer_refunds WHERE consumer_id = ? ORDER BY initiated_at DESC, refund_id DESC", (cid,))
        now = clock.now_iso()
        for r in out:
            r["overdue"] = r["status"] == "INITIATED" and r["expected_by"] < now
        return {"consumer_id": cid, "refunds": out}

    @app.post("/mock/consumer/{cid}/disputes", status_code=201)
    def raise_dispute(cid: str, body: DisputeIn, idempotency_key: Optional[str] = Header(default=None)):
        def fn(c):
            known(c, cid)
            t = c.execute("SELECT * FROM consumer_txns WHERE txn_id = ? AND consumer_id = ?", (body.txn_id, cid)).fetchone()
            if t is None:
                return 404, {"error": "TXN_NOT_FOUND"}
            open_ = c.execute("SELECT dispute_id FROM consumer_disputes WHERE txn_id = ? AND status = 'OPEN'", (body.txn_id,)).fetchone()
            if open_:
                return 200, {"dispute_id": open_[0], "status": "OPEN", "existing": True}
            did = next_id(c, "DSP")
            c.execute("INSERT INTO consumer_disputes VALUES (?,?,?,?,?)", (did, cid, body.txn_id, "OPEN", clock.now_iso()))
            return 201, {"dispute_id": did, "status": "OPEN"}
        return idem_write(idempotency_key, fn)

    @app.delete("/mock/consumer/{cid}/disputes/{did}")
    def withdraw_dispute(cid: str, did: str, idempotency_key: Optional[str] = Header(default=None)):
        def fn(c):
            c.execute("UPDATE consumer_disputes SET status = 'WITHDRAWN' WHERE dispute_id = ? AND consumer_id = ?", (did, cid))
            return 200, {"dispute_id": did, "status": "WITHDRAWN"}
        return idem_write(idempotency_key, fn)

    @app.post("/mock/consumer/{cid}/messages", status_code=201)
    def message(cid: str, body: MessageIn, idempotency_key: Optional[str] = Header(default=None)):
        def fn(c):
            known(c, cid)
            mid = next_id(c, "CM")
            c.execute("INSERT INTO consumer_messages VALUES (?,?,?,?,?)", (mid, cid, body.channel, body.body, clock.now_iso()))
            return 201, {"message_id": mid, "status": "SENT"}
        return idem_write(idempotency_key, fn)

    # -- scenario controls (the outside world; never an agent action) --------------------------------------------------------
    @app.post("/mock/consumer/control/txn/{txn_id}")
    def set_txn(txn_id: str, body: StatusIn):
        c = db()
        try:
            c.execute("UPDATE consumer_txns SET status = ? WHERE txn_id = ?", (body.status, txn_id))
            return {"ok": True}
        finally:
            c.close()

    @app.post("/mock/consumer/control/refund/{refund_id}")
    def set_refund(refund_id: str, body: StatusIn):
        c = db()
        try:
            c.execute("UPDATE consumer_refunds SET status = ? WHERE refund_id = ?", (body.status, refund_id))
            return {"ok": True}
        finally:
            c.close()

    return app
