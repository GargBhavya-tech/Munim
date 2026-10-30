"""A merchant types to Munim (the text twin of /ws/voice).

POST /api/merchants/{mid}/messages  body {"text": str, "language": str|null, "channel": str|null}
  -> {"event_id": str, "dropped": bool, "replies": [{"seq", "case_id", "language", "text"}]}   (seq = the ledger entry, for POST /api/voice/speak/{seq})

The text is redacted at this gateway (fail-closed: an unredactable message is dropped and the merchant is asked to repeat, nothing is
stored) and becomes a MERCHANT_MESSAGE event; the dispatcher and engine take it from there. ``replies`` are the MESSAGE_SENT entries the
engine wrote while handling it, i.e. only text the grounding gate let through. The server never builds a reply itself.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.deps import sent_replies, write_db
from core.engine.events import append_event
from core.engine.intents import top_intent
from core.memory.redact import PLEASE_REPEAT, redact_or_drop
from core.memory.store import next_id, write_txn

router = APIRouter(prefix="/api/merchants")
MAX_TEXT = 2000


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    language: Optional[str] = None
    channel: Optional[str] = None


@router.post("/{mid}/messages")
def merchant_message(mid: str, body: MessageIn, request: Request):
    st = request.app.state
    with st.lock:
        with write_db(request) as conn:
            if conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,)).fetchone() is None:
                raise HTTPException(404, {"error": "UNKNOWN_MERCHANT"})
            red = redact_or_drop(body.text)
            if red is None:
                return {"event_id": None, "dropped": True, "replies": [], "repeat_prompt": PLEASE_REPEAT.get("en")}
            after = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
            with write_txn(conn):
                event_id = next_id(conn, "MM")
                payload = {"text": red.text, "language": body.language, "channel": body.channel or "app", "secret_shared": red.secret_shared,
                           "intent": top_intent(red.text)}
                append_event(conn, event_id=event_id, type="MERCHANT_MESSAGE", payload=payload, ts_sim=st.clock.now_iso(), merchant_id=mid)
        st.after_write()
        with write_db(request) as conn:
            replies = sent_replies(conn, mid, after)
    return {"event_id": event_id, "dropped": False, "replies": replies}


MAX_IMAGE_BYTES = 3_000_000


@router.post("/{mid}/screenshot")
async def payment_screenshot(mid: str, request: Request):
    """Scam Shield (#86): the merchant shows a customer's payment screen. Body: the image bytes (Content-Type image/*).
    Only the extracted DRAFT fields are kept; the image itself is never stored. The verdict is made by code against the merchant's
    own transactions and comes back in ``replies`` like any other reply."""
    st = request.app.state
    image = await request.body()
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise HTTPException(422, {"error": "BAD_IMAGE"})
    mime = request.headers.get("content-type", "image/png").split(";")[0]
    reader = getattr(st, "screenshot_reader", None)
    if reader is None:
        from core.vision.read_screenshot import SyntheticSvgReader
        if mime != "image/svg+xml":
            raise HTTPException(503, {"error": "NO_VISION_READER", "detail": "only the synthetic SVG reader is configured"})
        reader = SyntheticSvgReader()
    draft = reader.read(image, mime)
    del image
    with st.lock:
        with write_db(request) as conn:
            if conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,)).fetchone() is None:
                raise HTTPException(404, {"error": "UNKNOWN_MERCHANT"})
            after = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
            with write_txn(conn):
                event_id = next_id(conn, "MM")
                payload = {"text": "[payment screenshot]", "channel": "app", "intent": "VERIFY_PAYMENT", "screenshot": draft.as_payload()}
                append_event(conn, event_id=event_id, type="MERCHANT_MESSAGE", payload=payload, ts_sim=st.clock.now_iso(), merchant_id=mid)
        st.after_write()
        with write_db(request) as conn:
            replies = sent_replies(conn, mid, after)
    return {"event_id": event_id, "reading": draft.as_payload(), "replies": replies}
