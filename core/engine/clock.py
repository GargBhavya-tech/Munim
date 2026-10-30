"""The only module allowed to read the wall clock (besides core/eval/latency.py).

Everything time-dependent takes a ``SimClock`` so tests can simulate hours in
milliseconds and demos can jump "48 hours later". All sim datetimes are aware
and in IST so ISO strings sort correctly and quiet hours are unambiguous.
"""
from __future__ import annotations

import heapq
import itertools
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

IST = timezone(timedelta(hours=5, minutes=30), "IST")
DEFAULT_START = datetime(2026, 10, 31, 9, 0, tzinfo=IST)


def iso(dt: datetime) -> str:
    """Canonical string form stored in the database (always IST)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: sim time is always timezone-aware")
    return dt.astimezone(IST).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(IST)


class SimClock:
    """Simulated time with timers. ``advance`` fires due callbacks in order, once."""

    def __init__(self, start: datetime = DEFAULT_START):
        if start.tzinfo is None:
            raise ValueError("SimClock needs an aware start time")
        self._now = start.astimezone(IST)
        self._lock = threading.RLock()
        self._heap: list[tuple[datetime, int, int]] = []
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._ids = itertools.count(1)

    def now(self) -> datetime:
        return self._now

    def now_iso(self) -> str:
        return iso(self._now)

    def call_at(self, when: datetime, fn: Callable[[], None]) -> int:
        """Run ``fn`` when sim time reaches ``when``. Returns a handle for ``cancel``."""
        if when.tzinfo is None:
            raise ValueError("naive datetime")
        with self._lock:
            handle = next(self._ids)
            self._callbacks[handle] = fn
            heapq.heappush(self._heap, (when.astimezone(IST), handle, handle))
            return handle

    def cancel(self, handle: int) -> bool:
        with self._lock:
            return self._callbacks.pop(handle, None) is not None

    def advance(self, td: timedelta) -> None:
        if td < timedelta(0):
            raise ValueError("cannot advance backwards; use set()")
        target = self._now + td
        while True:
            with self._lock:
                if not self._heap or self._heap[0][0] > target:
                    break
                due, _, handle = heapq.heappop(self._heap)
                fn = self._callbacks.pop(handle, None)
                if fn is None:  # cancelled
                    continue
                self._now = max(self._now, due)
            fn()  # outside the lock: callbacks may schedule more timers
        with self._lock:
            self._now = target

    def set(self, dt: datetime) -> None:
        """Jump to ``dt``. Forward jumps fire due timers; backward jumps just move the clock."""
        dt = dt.astimezone(IST)
        if dt >= self._now:
            self.advance(dt - self._now)
        else:
            with self._lock:
                self._now = dt


class RealClock:
    """Wall clock, only for ledger ``ts_real`` and latency metrics, never for logic."""

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def monotonic(self) -> float:
        import time

        return time.monotonic()
