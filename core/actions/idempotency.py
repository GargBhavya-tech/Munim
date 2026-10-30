"""Deterministic idempotency keys (#24).

    key = sha256(f"{case_id}|{action}|{canonical_json(args)}|{step_seq}")

The key is a pure function of the logical action, so a retry, a restart or a second process that
re-derives it gets the same key, and the mock's exactly-once write path replays instead of re-executing.
Never uuid4(), never the wall clock.
"""
from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from decimal import Decimal
from typing import Any

from core.audit.ledger import canonical_json
from core.engine.clock import SimClock

_MONEY_KEYS = ("amount", "amount_paise")


def _is_money(key: str) -> bool:
    return key in _MONEY_KEYS or key.endswith("_paise") or key.endswith("_amount")


def _norm(value: Any, key: str = "") -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if _is_money(key):
            return int(round(value))          # money is integer paise, always
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {unicodedata.normalize("NFC", str(k)): _norm(v, str(k)) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_norm(v, key) for v in value]
    return value


def normalise_args(args: dict | None) -> dict:
    """Canonical form of an args dict: 100.0 == 100, None-valued keys dropped, strings NFC, key order irrelevant."""
    return _norm(dict(args or {}))


def make_key(case_id: str | None, action: str, args: dict | None, step_seq: int = 0) -> str:
    """The idempotency key. ``args`` is normalised here too, so callers cannot get it wrong."""
    body = canonical_json(normalise_args(args))
    raw = f"{case_id if case_id is not None else '-'}|{action}|{body}|{step_seq}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Engine-side table: key -> (action_id, result_json). Writes join the caller's transaction."""

    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT key, action_id, result_json, created_at FROM idempotency WHERE key = ?", (key,)).fetchone()

    def reserve(self, key: str, action_id: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO idempotency(key, action_id, result_json, created_at) VALUES (?,?,NULL,?)",
                                (key, action_id, self.clock.now_iso()))
        return cur.rowcount == 1

    def complete(self, key: str, result_json: str) -> None:
        self.conn.execute("UPDATE idempotency SET result_json = ? WHERE key = ?", (result_json, key))
