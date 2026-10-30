import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require_w

router = APIRouter(prefix="/mock/tickets")
Conn = Annotated[sqlite3.Connection, Depends(read_db("tickets"))]
TYPES = {"DEVICE", "SETTLEMENT", "KYC", "BILLING", "COMPLAINT", "DISPUTE", "OTHER"}


class TicketIn(BaseModel):
    merchant_id: str
    type: str
    system_object: Optional[str] = None
    summary: Optional[str] = None
    case_id: Optional[str] = None


@router.get("")
def list_tickets(mid: str, conn: Conn):
    return {"merchant_id": mid, "tickets": fetch_all(conn, "SELECT * FROM tickets WHERE merchant_id = ? ORDER BY created_at, ticket_id", (mid,))}


@router.post("")
def create_ticket(body: TicketIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        require_w(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        if body.type not in TYPES:
            raise MockError(422, "UNKNOWN_TICKET_TYPE")
        tid = new_id(conn, "T")
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?)", (tid, body.merchant_id, body.type, "OPEN", body.system_object, body.summary, ctx.now_iso(), body.case_id))
        return Result(201, {"ticket_id": tid, "status": "OPEN"})

    return execute_write(ctx, "tickets", idem, {"op": "tickets.create", "body": body.model_dump()}, fn)


@router.delete("/{tid}")
def withdraw_ticket(tid: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        t = require_w(conn, "SELECT * FROM tickets WHERE ticket_id = ?", (tid,), "TICKET_NOT_FOUND")
        if t["status"] == "WITHDRAWN":
            return Result(200, {"ticket_id": tid, "status": "WITHDRAWN"})
        if t["status"] != "OPEN":
            raise MockError(409, "NOT_WITHDRAWABLE", t["status"])
        conn.execute("UPDATE tickets SET status='WITHDRAWN' WHERE ticket_id = ?", (tid,))
        return Result(200, {"ticket_id": tid, "status": "WITHDRAWN"})

    return execute_write(ctx, "tickets", idem, {"op": "tickets.withdraw", "tid": tid}, fn)
