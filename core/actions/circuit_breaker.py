"""Per-system circuit breaker (#28). State lives in ``breaker_state`` so a restart does not forget an outage.

closed --3 consecutive failed calls--> open --30 s of SIM time--> half-open (one probe) --success--> closed
                                                                            \\--failure--> open (cooldown restarts)

A "failure" is a logical call that still failed after its retries (timeout / 5xx). Business 4xx answers
mean the system is alive and count as success. Every state change is ledgered (kind STATE, case-less).
"""
from __future__ import annotations

from core.audit.ledger import Ledger
from core.engine.clock import SimClock, parse_iso
from core.memory.store import write_txn

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, conn, clock: SimClock, ledger: Ledger, failure_threshold: int = 3, cooldown_s: float = 30.0):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        self.failure_threshold, self.cooldown_s = failure_threshold, cooldown_s

    def _row(self, system: str) -> dict:
        r = self.conn.execute("SELECT system, state, failures, opened_at FROM breaker_state WHERE system = ?", (system,)).fetchone()
        return dict(r) if r else {"system": system, "state": CLOSED, "failures": 0, "opened_at": None}

    def state(self, system: str) -> str:
        return self._row(system)["state"]

    def failures(self, system: str) -> int:
        return self._row(system)["failures"]

    def _set(self, system: str, state: str, failures: int, opened_at: str | None, prev: str, why: str) -> None:
        with write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO breaker_state(system, state, failures, opened_at) VALUES (?,?,?,?)"
                " ON CONFLICT(system) DO UPDATE SET state=excluded.state, failures=excluded.failures, opened_at=excluded.opened_at",
                (system, state, failures, opened_at))
            if prev != state:
                self.ledger.append(kind="STATE", actor="SYSTEM", payload={"breaker": system, "from": prev, "to": state, "why": why, "failures": failures})

    def allow(self, system: str) -> tuple[bool, bool]:
        """(may call, is probe). OPEN blocks until the cooldown passed on the sim clock, then lets ONE probe through."""
        r = self._row(system)
        if r["state"] == CLOSED:
            return True, False
        if r["state"] == OPEN:
            elapsed = (self.clock.now() - parse_iso(r["opened_at"])).total_seconds()
            if elapsed >= self.cooldown_s:
                self._set(system, HALF_OPEN, r["failures"], r["opened_at"], OPEN, f"cooldown {self.cooldown_s:g}s elapsed")
                return True, True
            return False, False
        # HALF_OPEN persisted: a probe was released and never reported (crash) -> the next caller is the probe
        return True, True

    def retry_after_s(self, system: str) -> float:
        r = self._row(system)
        if r["state"] != OPEN or not r["opened_at"]:
            return 0.0
        return max(0.0, self.cooldown_s - (self.clock.now() - parse_iso(r["opened_at"])).total_seconds())

    def record_success(self, system: str) -> None:
        r = self._row(system)
        if r["state"] != CLOSED or r["failures"]:
            self._set(system, CLOSED, 0, None, r["state"], "call succeeded")

    def record_failure(self, system: str) -> None:
        r = self._row(system)
        if r["state"] == HALF_OPEN:
            self._set(system, OPEN, r["failures"] + 1, self.clock.now_iso(), HALF_OPEN, "probe failed")
            return
        n = r["failures"] + 1
        if n >= self.failure_threshold:
            self._set(system, OPEN, n, self.clock.now_iso(), r["state"], f"{n} consecutive failures")
        else:
            self._set(system, CLOSED, n, None, r["state"], "failure")
