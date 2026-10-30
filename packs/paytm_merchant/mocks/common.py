"""Shared plumbing for the 15 mock systems: context, chaos, and the exactly-once write path.

Every mutating endpoint goes through ``execute_write``:

  * requires ``Idempotency-Key``
  * stores ``(key -> status, body)`` in the SAME transaction as the write
  * replays the stored response for a repeated key without executing again
  * rejects the same key reused with a different request (422)
  * applies chaos (latency, error500 before commit, timeout AFTER commit, duplicate webhooks)
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Callable

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core.engine.clock import SimClock, iso
from core.engine.events import append_event
from core.memory.store import connect, next_id

SYSTEMS = (
    "merchant", "kyc", "bank", "device", "payments", "settlement", "pricing", "plans", "risk",
    "tickets", "scheduler", "comms", "lender", "refunds", "closures",
)
CHAOS_MODES = ("latency", "error500", "timeout_after_commit", "duplicate_webhook")


class MockError(Exception):
    """A business-rule failure. Stored and replayed like any other result."""

    def __init__(self, status: int, code: str, detail: str | None = None):
        self.status, self.code, self.detail = status, code, detail


@dataclass
class EventSpec:
    type: str
    payload: dict
    merchant_id: str | None = None


@dataclass
class Result:
    status: int
    body: dict
    events: list[EventSpec] = field(default_factory=list)


@dataclass
class MockContext:
    db_path: Path | str
    clock: SimClock
    chaos_seed: int = 0

    def __post_init__(self) -> None:
        self._rngs: dict[tuple[str, str], random.Random] = {}
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def draw(self, system: str, mode: str, rate: float) -> bool:
        """Deterministic per-(system, mode) random stream, so chaos runs are reproducible."""
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        with self._lock:
            rng = self._rngs.setdefault((system, mode), random.Random(f"{self.chaos_seed}:{system}:{mode}"))
            return rng.random() < rate

    def now(self):
        return self.clock.now()

    def now_iso(self) -> str:
        return self.clock.now_iso()

    def in_hours(self, hours: float) -> str:
        return iso(self.clock.now() + timedelta(hours=hours))


def get_ctx(request: Request) -> MockContext:
    return request.app.state.ctx


Ctx = Annotated[MockContext, Depends(get_ctx)]
Idem = Annotated[str | None, Header(alias="Idempotency-Key")]


def _load_chaos(conn: sqlite3.Connection, system: str) -> dict[str, tuple[float, float | None]]:
    out: dict[str, tuple[float, float | None]] = {}
    for r in conn.execute("SELECT system, mode, rate, param FROM chaos WHERE system IN ('*', ?) ORDER BY system ASC", (system,)):
        out[r["mode"]] = (r["rate"], r["param"])  # '*' sorts before letters, so specific rows are applied last and win
    return out


def _pre_chaos(ctx: MockContext, conn: sqlite3.Connection, system: str) -> dict:
    chaos = _load_chaos(conn, system)
    if "latency" in chaos:
        rate, ms = chaos["latency"]
        if ctx.draw(system, "latency", rate):
            time.sleep((ms or 0) / 1000.0)
    if "error500" in chaos and ctx.draw(system, "error500", chaos["error500"][0]):
        raise HTTPException(status_code=500, detail={"error": "CHAOS_ERROR500", "system": system})
    return chaos


def read_db(system: str):
    """FastAPI dependency factory for read endpoints (chaos applies to reads too)."""

    def dep(ctx: Ctx):
        conn = ctx.connect()
        try:
            _pre_chaos(ctx, conn, system)
            yield conn
        finally:
            conn.close()

    return dep


def fetch_one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def fetch_all(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def require(conn: sqlite3.Connection, sql: str, args: tuple, code: str) -> dict:
    row = fetch_one(conn, sql, args)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": code})
    return row


def require_w(conn: sqlite3.Connection, sql: str, args: tuple, code: str) -> dict:
    """Like ``require`` but raises MockError so the 404 is stored and replayed with the key."""
    row = fetch_one(conn, sql, args)
    if row is None:
        raise MockError(404, code)
    return row


def new_id(conn: sqlite3.Connection, prefix: str) -> str:
    return next_id(conn, prefix)


def token(key: str, n: int = 16) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:n]


def _request_hash(fingerprint: dict) -> str:
    return hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _response(status: int, body: dict, replay: bool = False) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Idempotent-Replay": "true"} if replay else None)


def execute_write(ctx: MockContext, system: str, key: str | None, fingerprint: dict, fn: Callable[[sqlite3.Connection], Result]) -> JSONResponse:
    if not key:
        raise HTTPException(status_code=400, detail={"error": "IDEMPOTENCY_KEY_REQUIRED"})
    conn = ctx.connect()
    try:
        chaos = _pre_chaos(ctx, conn, system)
        req_hash = _request_hash(fingerprint)
        conn.execute("BEGIN IMMEDIATE")
        replay = False
        try:
            stored = fetch_one(conn, "SELECT request_hash, status_code, body_json FROM mock_idempotency WHERE system = ? AND key = ?", (system, key))
            if stored:
                if stored["request_hash"] != req_hash:
                    conn.execute("ROLLBACK")
                    return _response(422, {"error": "IDEMPOTENCY_KEY_REUSED", "detail": "same key, different request"})
                status, body, replay = stored["status_code"], json.loads(stored["body_json"]), True
            else:
                conn.execute("SAVEPOINT w")
                try:
                    result = fn(conn)
                    conn.execute("RELEASE w")
                except MockError as e:
                    conn.execute("ROLLBACK TO w")
                    conn.execute("RELEASE w")
                    result = Result(e.status, {"error": e.code, **({"detail": e.detail} if e.detail else {})})
                status, body = result.status, result.body
                conn.execute(
                    "INSERT INTO mock_idempotency(system, key, request_hash, status_code, body_json, created_at) VALUES (?,?,?,?,?,?)",
                    (system, key, req_hash, status, json.dumps(body, sort_keys=True), ctx.now_iso()),
                )
                dup = "duplicate_webhook" in chaos and ctx.draw(system, "duplicate_webhook", chaos["duplicate_webhook"][0])
                for ev in result.events:
                    eid = f"{system}:{key}:{ev.type}"
                    for attempt in range(2 if dup else 1):
                        res = append_event(conn, event_id=eid, type=ev.type, payload=ev.payload, ts_sim=ctx.now_iso(), merchant_id=ev.merchant_id)
                        if not res.inserted:
                            conn.execute("INSERT INTO counters(name, n) VALUES ('dup_webhooks_dropped', 1) ON CONFLICT(name) DO UPDATE SET n = n + 1")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if "timeout_after_commit" in chaos and ctx.draw(system, "timeout_after_commit", chaos["timeout_after_commit"][0]):
            return _response(504, {"error": "CHAOS_TIMEOUT_AFTER_COMMIT", "system": system})
        return _response(status, body, replay)
    finally:
        conn.close()
