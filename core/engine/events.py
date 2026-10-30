"""Append-only event log, deduplicated by ``event_id`` (never by payload)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

EVENT_TYPES = frozenset({
    "SIGNUP_STALLED", "KYC_REJECTED", "KYC_SUBMITTED", "BANK_VERIFICATION_FAILED", "DEVICE_DELIVERED",
    "DEVICE_HEARTBEAT_LOST", "DEVICE_DEGRADING", "PAYMENT_SUCCESS", "PAYMENT_FAILED", "SETTLEMENT_SETTLED",
    "SETTLEMENT_HELD", "SETTLEMENT_FAILED", "MERCHANT_MESSAGE", "TIMER_FIRED", "HUMAN_RETURNED", "USAGE_DROP",
    "NEW_LEAD", "LEAD_REPLY", "VISIT_LOGGED",
})


@dataclass(frozen=True)
class AppendResult:
    inserted: bool
    seq: int | None


def append_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    type: str,
    payload: dict,
    ts_sim: str,
    merchant_id: str | None = None,
    case_id: str | None = None,
) -> AppendResult:
    """Insert one event. A repeated ``event_id`` is ignored (duplicate webhook), not an error.

    Works inside the caller's transaction, or on its own (a single INSERT is atomic).
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO events(event_id, merchant_id, case_id, type, payload_json, ts_sim) VALUES (?,?,?,?,?,?)",
        (event_id, merchant_id, case_id, type, json.dumps(payload, sort_keys=True, ensure_ascii=False), ts_sim),
    )
    if cur.rowcount == 1:
        return AppendResult(True, cur.lastrowid)
    return AppendResult(False, None)


def count_events(conn: sqlite3.Connection, type: str | None = None) -> int:
    if type:
        return conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (type,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
