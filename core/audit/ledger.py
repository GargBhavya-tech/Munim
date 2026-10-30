"""Tamper-evident, append-only audit ledger.

    h_0 = SHA256("MUNIM-GENESIS")
    h_n = SHA256(h_{n-1} || canonical_json(entry_n minus volatile fields))

Volatile fields (``ts_real``, anything ending in ``latency_ms``) are stored but excluded from the
hash so replays verify. Canonical JSON is sorted-keys, no spaces, UTF-8.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from core.engine.clock import RealClock, SimClock

GENESIS_HASH = hashlib.sha256(b"MUNIM-GENESIS").hexdigest()

KINDS = frozenset({
    "EVENT", "CONTEXT_HASH", "DECISION", "VERIFY", "POLICY", "ACTION_INTENT", "ACTION_RESULT", "COMPENSATION",
    "MESSAGE_SENT", "ESCALATION", "HUMAN_ACTION", "STATE",
})


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_volatile(key: str) -> bool:
    return key == "ts_real" or key.endswith("latency_ms")


def strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if not _is_volatile(k)}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


def hashed_view(*, seq: int, case_id, ts_sim, actor, role, kind, payload, policy_hash, model_id, prompt_hash) -> dict:
    return {
        "seq": seq, "case_id": case_id, "ts_sim": ts_sim, "actor": actor, "role": role, "kind": kind,
        "payload": strip_volatile(payload), "policy_hash": policy_hash, "model_id": model_id, "prompt_hash": prompt_hash,
    }


def chain_hash(prev_hash: str, view: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(view)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    case_id: str | None
    ts_sim: str
    ts_real: str
    actor: str
    role: str | None
    kind: str
    payload: dict
    policy_hash: str | None
    model_id: str | None
    prompt_hash: str | None
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    first_mismatch: int | None = None
    reason: str | None = None


def _row_to_entry(r: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(r["seq"], r["case_id"], r["ts_sim"], r["ts_real"], r["actor"], r["role"], r["kind"],
                       json.loads(r["payload_json"]), r["policy_hash"], r["model_id"], r["prompt_hash"], r["prev_hash"], r["hash"])


class Ledger:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: SimClock,
        real_clock: RealClock | None = None,
        policy_hash: Callable[[], str | None] | None = None,
    ):
        self.conn = conn
        self.clock = clock
        self.real_clock = real_clock or RealClock()
        self._policy_hash = policy_hash or (lambda: None)

    def append(
        self,
        *,
        kind: str,
        actor: str,
        case_id: str | None = None,
        role: str | None = None,
        payload: dict | None = None,
        policy_hash: str | None = None,
        model_id: str | None = None,
        prompt_hash: str | None = None,
    ) -> LedgerEntry:
        """Single writer: takes the write lock (BEGIN IMMEDIATE) unless the caller already holds it."""
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}")
        payload = payload or {}
        json.dumps(payload)  # fail early if not serialisable
        own = not self.conn.in_transaction
        if own:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            last = self.conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            seq = last["seq"] + 1 if last else 1
            prev = last["hash"] if last else GENESIS_HASH
            ts_sim = self.clock.now_iso()
            ts_real = self.real_clock.now_iso()
            policy_hash = policy_hash if policy_hash is not None else self._policy_hash()
            view = hashed_view(seq=seq, case_id=case_id, ts_sim=ts_sim, actor=actor, role=role, kind=kind, payload=payload,
                               policy_hash=policy_hash, model_id=model_id, prompt_hash=prompt_hash)
            h = chain_hash(prev, view)
            self.conn.execute(
                "INSERT INTO ledger(seq, case_id, ts_sim, ts_real, actor, role, kind, payload_json, policy_hash, model_id, prompt_hash, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (seq, case_id, ts_sim, ts_real, actor, role, kind, canonical_json(payload), policy_hash, model_id, prompt_hash, prev, h),
            )
            if own:
                self.conn.execute("COMMIT")
        except BaseException:
            if own:
                self.conn.execute("ROLLBACK")
            raise
        return LedgerEntry(seq, case_id, ts_sim, ts_real, actor, role, kind, payload, policy_hash, model_id, prompt_hash, prev, h)

    def entries(self, case_id: str | None = None) -> list[LedgerEntry]:
        if case_id is None:
            rows = self.conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM ledger WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()
        return [_row_to_entry(r) for r in rows]


def verify_chain(conn: sqlite3.Connection) -> VerifyResult:
    """Recompute every hash in order; report the FIRST sequence number that does not match."""
    prev = GENESIS_HASH
    expected_seq = 1
    count = 0
    for r in conn.execute("SELECT * FROM ledger ORDER BY seq"):
        e = _row_to_entry(r)
        count += 1
        if e.seq != expected_seq:
            return VerifyResult(False, count, expected_seq, f"sequence gap: expected {expected_seq}, found {e.seq}")
        view = hashed_view(seq=e.seq, case_id=e.case_id, ts_sim=e.ts_sim, actor=e.actor, role=e.role, kind=e.kind,
                           payload=e.payload, policy_hash=e.policy_hash, model_id=e.model_id, prompt_hash=e.prompt_hash)
        h = chain_hash(prev, view)
        if e.prev_hash != prev:
            return VerifyResult(False, count, e.seq, "prev_hash does not match the previous entry")
        if e.hash != h:
            return VerifyResult(False, count, e.seq, "hash does not match the entry contents")
        prev = h
        expected_seq += 1
    return VerifyResult(True, count)
