"""Break Munim arena (#132). NOT registered in core/api/app.py by this ticket; see the report for the registration lines.

GET  /api/arena                          -> {label, state, levels, budget, leaderboard}                   public
GET  /api/arena/leaderboard              -> {label, leaderboard}                                          public
POST /api/arena/players   {nickname}     -> {player_id, token, nickname, levels}   400 NICKNAME_REJECTED, 409 NICKNAME_TAKEN, 503 ARENA_STOPPED / ARENA_FULL
GET  /api/arena/players/{token}          -> {player_id, nickname, attempts, levels, state}               404 UNKNOWN_PLAYER
POST /api/arena/players/{token}/attempts {text, language?}
                                         -> {replies, attempt_no, cleared_now, levels, state, budget}    429 RATE_LIMITED, 503 ARENA_STOPPED
POST /api/arena/admin/kill | /admin/revive   header X-Arena-Admin: <app.state.arena_admin_token>        403 when the token is unset or wrong

The arena object is ``app.state.arena`` (core.judge.arena.Arena). Without it every route answers 404 ARENA_DISABLED.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.judge.arena import (LABEL, Arena, ArenaError, ArenaFull, ArenaStopped, NicknameRejected, NicknameTaken, RateLimited,
                              UnknownPlayer)

router = APIRouter(prefix="/api/arena")
_STATUS = {NicknameRejected: 400, NicknameTaken: 409, ArenaStopped: 503, ArenaFull: 503, RateLimited: 429, UnknownPlayer: 404}


class JoinIn(BaseModel):
    nickname: str = Field(max_length=64)


class AttemptIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: Optional[str] = None


def _arena(request: Request) -> Arena | None:
    return getattr(request.app.state, "arena", None)


def _fail(e: ArenaError) -> JSONResponse:
    return JSONResponse({"detail": e.as_json()}, status_code=_STATUS.get(type(e), 400))


def _disabled() -> JSONResponse:
    return JSONResponse({"detail": {"error": "ARENA_DISABLED", "label": LABEL}}, status_code=404)


@router.get("")
def info(request: Request):
    a = _arena(request)
    return a.info() if a else _disabled()


@router.get("/leaderboard")
def leaderboard(request: Request):
    a = _arena(request)
    return {"label": LABEL, "leaderboard": a.leaderboard()} if a else _disabled()


@router.post("/players")
def join(body: JoinIn, request: Request):
    a = _arena(request)
    if not a:
        return _disabled()
    try:
        return a.join(body.nickname)
    except ArenaError as e:
        return _fail(e)


@router.get("/players/{token}")
def player(token: str, request: Request):
    a = _arena(request)
    if not a:
        return _disabled()
    try:
        return a.player(token)
    except ArenaError as e:
        return _fail(e)


@router.post("/players/{token}/attempts")
def attempt(token: str, body: AttemptIn, request: Request):
    a = _arena(request)
    if not a:
        return _disabled()
    try:
        return a.attempt(token, body.text, body.language)
    except ArenaError as e:
        return _fail(e)


def _admin(request: Request, header: str | None) -> bool:
    want = getattr(request.app.state, "arena_admin_token", None)
    return bool(want) and header is not None and hmac.compare_digest(str(want), header)


@router.post("/admin/kill")
def kill(request: Request, x_arena_admin: str | None = Header(default=None)):
    a = _arena(request)
    if not a:
        return _disabled()
    if not _admin(request, x_arena_admin):
        return JSONResponse({"detail": {"error": "FORBIDDEN", "label": LABEL}}, status_code=403)
    a.kill()
    return {"label": LABEL, "state": a.state()}


@router.post("/admin/revive")
def revive(request: Request, x_arena_admin: str | None = Header(default=None)):
    a = _arena(request)
    if not a:
        return _disabled()
    if not _admin(request, x_arena_admin):
        return JSONResponse({"detail": {"error": "FORBIDDEN", "label": LABEL}}, status_code=403)
    a.revive()
    return {"label": LABEL, "state": a.state()}
