"""Push-to-talk over a WebSocket (#39). One utterance per exchange:

  client -> {"merchant_id": "M-0001", "language": "unknown", "hinglish": false}      (text frame)
  client -> <WAV bytes>                                                              (binary frame)
  server -> {"type": "transcript", "replies": [{"seq", "case_id", "language", "text"}], "text": ..., "language": ..., "confidence": ..., "low_confidence": ..., "event_id": ..., "latency_ms": {...}}
         or {"type": "dropped", "reason": ..., "repeat_prompt": ...}

The server never sends back a decision or an action: the transcript becomes a MERCHANT_MESSAGE event and the engine takes it from there.
Raw audio is not stored. Without a configured processor the endpoint refuses the connection.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from core.api.deps import open_ro, read_db, sent_replies

router = APIRouter()
MAX_AUDIO_BYTES = 4_000_000          # about two minutes of 16 kHz 16-bit mono; a push-to-talk clip is a few seconds


@router.websocket("/ws/voice")
async def voice(ws: WebSocket):
    processor = getattr(ws.app.state, "voice", None)
    if processor is None:
        await ws.close(code=1011)
        return
    await ws.accept()
    try:
        while True:
            setup = await ws.receive_json()
            wav = await ws.receive_bytes()
            merchant_id = setup.get("merchant_id")
            if not merchant_id or len(wav) > MAX_AUDIO_BYTES:
                await ws.send_json({"type": "dropped", "reason": "BAD_REQUEST", "repeat_prompt": None})
                continue
            ro = open_ro(ws.app.state.db_path)
            try:                                              # closed on every path, including a processor that raises
                after = ro.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
                with ws.app.state.lock:
                    r = processor.process(wav, merchant_id, language_code=setup.get("language") or "unknown", hinglish=bool(setup.get("hinglish")),
                                          consent_store_audio=bool(setup.get("consent_store_audio")), case_id=setup.get("case_id"))
                if not r.dropped:
                    ws.app.state.after_write()
                    replies = sent_replies(ro, merchant_id, after)
            finally:
                ro.close()
            if r.dropped:
                await ws.send_json({"type": "dropped", "reason": r.reason, "repeat_prompt": r.repeat_prompt, "latency_ms": r.latency_ms})
                continue
            await ws.send_json({"type": "transcript", "replies": replies, "text": r.text, "language": r.language, "confidence": r.confidence, "low_confidence": r.low_confidence,
                                "secret_shared": r.secret_shared, "event_id": r.event_id, "intent": r.intent, "latency_ms": r.latency_ms})
    except WebSocketDisconnect:
        return


@router.post("/api/voice/speak/{seq}")
def speak(seq: int, request: Request, session_id: str, conn=Depends(read_db)):
    """Spoken replies. Voices ONLY a message the engine already sent (a MESSAGE_SENT ledger entry, i.e. text the grounding gate let through), by its ledger
    seq. There is no way to send this endpoint free text, so it cannot be used to make Munim say something the gate has not checked.
    Scoped to the caller: ``session_id`` is the phone's judge session, and the entry must be a message to THAT session's merchant, sent after the session
    started. Anything else is refused before the (paid) speech service is called."""
    from core.judge.session import UnknownSession, load_session
    processor = getattr(request.app.state, "voice", None)
    if processor is None:
        raise HTTPException(503, {"error": "VOICE_NOT_CONFIGURED"})
    try:
        session = load_session(conn, session_id)
    except UnknownSession:
        raise HTTPException(404, {"error": "UNKNOWN_SESSION"})
    row = conn.execute("SELECT l.kind, l.payload_json, c.merchant_id FROM ledger l LEFT JOIN cases c ON c.case_id = l.case_id WHERE l.seq = ?",
                       (seq,)).fetchone()
    if row is None:
        raise HTTPException(404, {"error": "NO_SUCH_ENTRY"})
    if row["kind"] != "MESSAGE_SENT":
        raise HTTPException(409, {"error": "NOT_A_SENT_MESSAGE"})
    if row["merchant_id"] != session.merchant_id or seq <= session.start_seq:
        raise HTTPException(403, {"error": "NOT_THIS_SESSIONS_MESSAGE"})
    payload = json.loads(row["payload_json"])
    text = payload.get("text")
    if not text:
        raise HTTPException(409, {"error": "NOTHING_TO_SPEAK"})
    audio, ms = processor.synthesize(text, payload.get("language") or "en-IN")
    return Response(content=audio, media_type="audio/wav", headers={"X-Tts-Latency-Ms": str(round(ms, 1))})
