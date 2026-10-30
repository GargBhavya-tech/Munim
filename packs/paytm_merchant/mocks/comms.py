import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .common import Ctx, Idem, MockError, Result, execute_write, fetch_all, new_id, read_db, require_w

router = APIRouter(prefix="/mock/comms")
Conn = Annotated[sqlite3.Connection, Depends(read_db("comms"))]
CHANNELS = {"app", "soundbox", "whatsapp", "sms", "voice", "email"}


class SendIn(BaseModel):
    merchant_id: Optional[str] = None      # a merchant (or, for the first-contact job, a lead id: leads have their own id namespace)
    lead_id: Optional[str] = None
    channel: str
    body: str
    language: Optional[str] = None
    case_id: Optional[str] = None


@router.get("")
def inbox(mid: str, conn: Conn):
    """The on-screen inbox. Nothing here ever leaves the machine."""
    return {"merchant_id": mid, "messages": fetch_all(conn, "SELECT * FROM comms_outbox WHERE merchant_id = ? ORDER BY ts, comm_id", (mid,))}


@router.post("/send")
def send(body: SendIn, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        if bool(body.merchant_id) == bool(body.lead_id):
            raise MockError(422, "ONE_RECIPIENT_REQUIRED")
        if body.lead_id:
            require_w(conn, "SELECT 1 FROM leads WHERE lead_id = ?", (body.lead_id,), "LEAD_NOT_FOUND")
        elif not conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (body.merchant_id,)).fetchone():
            require_w(conn, "SELECT 1 FROM leads WHERE lead_id = ?", (body.merchant_id,), "MERCHANT_NOT_FOUND")
        rcpt = body.lead_id or body.merchant_id
        if body.channel not in CHANNELS:
            raise MockError(422, "UNKNOWN_CHANNEL")
        cid = new_id(conn, "C")
        conn.execute("INSERT INTO comms_outbox VALUES (?,?,?,?,?,?,?,?)", (cid, rcpt, body.channel, body.language, body.body, body.case_id, "SENT", ctx.now_iso()))
        return Result(201, {"comm_id": cid, "status": "SENT"})

    return execute_write(ctx, "comms", idem, {"op": "comms.send", "body": body.model_dump()}, fn)


class NotifyIn(BaseModel):
    person_id: str
    body: str
    language: Optional[str] = None
    subject: Optional[str] = None


@router.post("/notify-staff")
def notify_staff(body: NotifyIn, ctx: Ctx, idem: Idem = None):
    """Internal staff note (rep or technician). Stored in the same outbox under the staff id, channel 'app', so the dashboard can show it."""
    def fn(conn):
        require_w(conn, "SELECT 1 FROM (SELECT rep_id AS pid FROM reps UNION SELECT tech_id FROM technicians) WHERE pid = ?", (body.person_id,), "PERSON_NOT_FOUND")
        cid = new_id(conn, "C")
        conn.execute("INSERT INTO comms_outbox VALUES (?,?,?,?,?,?,?,?)", (cid, body.person_id, "app", body.language, body.body, body.subject, "SENT", ctx.now_iso()))
        return Result(201, {"comm_id": cid, "status": "SENT"})

    return execute_write(ctx, "comms", idem, {"op": "comms.notify_staff", "body": body.model_dump()}, fn)
