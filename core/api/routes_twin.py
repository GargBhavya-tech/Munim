"""Guard-off twin (#131), demo only. NOT registered in core/api/app.py by this ticket; see the report for the registration lines.

POST /api/twin/start                          -> {banner, label, isolated, ...}  snapshot the live DB into a fresh isolated twin
POST /api/twin/reset                          -> same; a new snapshot replaces the twin
GET  /api/twin                                -> {banner, active, isolated}
POST /api/twin/messages {merchant_id, text, language?, session_id?}
                                              -> {banner, label, real: <production message response>, twin: <twin response, guards OFF>}

EVERY response, errors included, carries ``banner`` = "GUARDS OFF - SIMULATED TWIN". The routes answer 404 unless the demo build sets
``app.state.twin_enabled = True``; that switch only exposes these routes. It cannot turn any guard off on the real engine: guards-off exists
only inside core.judge.twin on the twin's own snapshot DB. The twin needs ``app.state.twin_client_factory(ledger) -> LLM client``.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.api.routes_messages import MAX_TEXT, MessageIn, merchant_message
from core.judge.twin import BANNER, Twin, twin_header

router = APIRouter(prefix="/api/twin")


class TwinMessageIn(BaseModel):
    merchant_id: str
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    language: Optional[str] = None
    session_id: Optional[str] = None        # Judge Mode: the real half goes through the judge session (its caps, its attempt count)


def _err(status: int, error: str, **extra) -> JSONResponse:
    return JSONResponse({"detail": {"error": error, **twin_header(), **extra}}, status_code=status)


def _gate(request: Request):
    st = request.app.state
    if not getattr(st, "twin_enabled", False):
        return _err(404, "TWIN_DISABLED")
    if getattr(st, "twin_client_factory", None) is None:
        return _err(503, "NO_TWIN_LLM")
    return None


def _twin_dir(st) -> Path:
    d = getattr(st, "twin_dir", None)
    if d is None:
        d = st.twin_dir = Path(tempfile.mkdtemp(prefix="munim-twin-"))
    return Path(d)


def _status(request: Request) -> dict:
    t = getattr(request.app.state, "twin", None)
    if t is not None:
        t.verify_isolation()
    return {**twin_header(), "active": t is not None, "isolated": t is not None}


def _build(request: Request) -> None:
    st = request.app.state
    old = getattr(st, "twin", None)
    if old is not None:
        old.close()
    n = getattr(st, "twin_seq", 0) + 1
    st.twin_seq = n
    st.twin = None
    st.twin = Twin(st.db_path, _twin_dir(st) / f"twin-{n}.db", st.clock.now(), st.twin_client_factory)


@router.get("")
def status(request: Request):
    if (g := _gate(request)) is not None:
        return g
    return _status(request)


@router.post("/start")
def start(request: Request):
    if (g := _gate(request)) is not None:
        return g
    with request.app.state.lock:
        _build(request)
        return _status(request)


@router.post("/reset")
def reset(request: Request):
    return start(request)


@router.post("/messages")
def messages(body: TwinMessageIn, request: Request):
    if (g := _gate(request)) is not None:
        return g
    st = request.app.state
    twin = getattr(st, "twin", None)
    if twin is None:
        return _err(409, "TWIN_NOT_STARTED")
    with st.lock:
        if body.session_id:                                  # a judge's turn: the same session flow as without the twin (caps first, attempt ledgered)
            from core.api.routes_judge import JudgeMessageIn, message as judge_message
            real = judge_message(body.session_id, JudgeMessageIn(text=body.text, language=body.language), request)
            if isinstance(real, JSONResponse):               # a cap refused the turn: nothing was sent to either side
                return real
        else:
            real = merchant_message(body.merchant_id, MessageIn(text=body.text, language=body.language, channel="app"), request)   # production path; a 404 stops here
        out = twin.handle(body.merchant_id, body.text, body.language)
    return {"banner": BANNER, "label": "SIMULATED", "real": real, "twin": out}
