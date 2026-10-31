"""Shop opening hours (service capability: the merchant profile knows shop hours).

A visit, a call and a swap are planned inside the hours a shop is open, and "the shop is closed" is a fact a visit can run into, so the hours have to be on record.
Two sources, never confused: REPORTED (the merchant or a person said so, ledgered) and ASSUMED (a typical day for the kind of shop, a stated assumption). Anything
that decides something from the hours can see which one it got.

Times are minutes of the day in the simulated clock's zone. A shop that is open past midnight is not modelled: ``close`` must be after ``open``.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.memory.store import write_txn

SCHEMA = ("CREATE TABLE IF NOT EXISTS shop_hours (merchant_id TEXT PRIMARY KEY, open_time TEXT NOT NULL, close_time TEXT NOT NULL, closed_days TEXT NOT NULL, "
          "source TEXT NOT NULL, updated_at TEXT NOT NULL)")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REPORTED, ASSUMED = "REPORTED", "ASSUMED"
SEGMENT_DEFAULTS: dict[str, tuple[str, str]] = {      # [ASSUMPTION] a typical trading day by kind of shop
    "kirana": ("08:00", "21:00"), "restaurant": ("11:00", "23:00"), "pharmacy": ("09:00", "22:00"), "salon": ("10:00", "20:00"),
    "electronics": ("10:00", "20:00"), "bakery": ("07:00", "21:00"), "chemist": ("09:00", "22:00"), "d2c": ("09:00", "21:00"),
}
FALLBACK = ("09:00", "20:00")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def to_minutes(hhmm: str) -> int:
    m = _HHMM.match(hhmm or "")
    if not m:
        raise ValueError(f"{hhmm!r} is not a time as HH:MM")
    return int(m[1]) * 60 + int(m[2])


@dataclass(frozen=True)
class Hours:
    open_min: int
    close_min: int
    closed_days: tuple[str, ...]
    source: str                                             # REPORTED | ASSUMED

    @property
    def open_minutes(self) -> int:
        return self.close_min - self.open_min

    @property
    def open_hours(self) -> int:
        return max(1, self.open_minutes // 60)

    def is_open(self, when: datetime) -> bool:
        if WEEKDAYS[when.weekday()] in self.closed_days:
            return False
        minute = when.hour * 60 + when.minute
        return self.open_min <= minute < self.close_min

    def next_open(self, after: datetime) -> datetime:
        """The first moment at or after ``after`` that the shop is open. A shop closed every day is never open: that raises, it is not guessed."""
        if len(set(self.closed_days)) >= 7:
            raise ValueError("the shop is closed every day")
        day = after.replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(8):
            opens = day + timedelta(minutes=self.open_min)
            closes = day + timedelta(minutes=self.close_min)
            if WEEKDAYS[day.weekday()] not in self.closed_days and after < closes:
                return max(opens, after)
            day += timedelta(days=1)
        raise ValueError("no opening time found in the next week")


def default_for(segment: str | None) -> Hours:
    o, c = SEGMENT_DEFAULTS.get(segment or "", FALLBACK)
    return Hours(to_minutes(o), to_minutes(c), (), ASSUMED)


def get_hours(conn: sqlite3.Connection, merchant_id: str) -> Hours:
    """The merchant's hours: reported ones if any, else the typical day for their kind of shop (marked ASSUMED). Safe on a read-only connection."""
    try:
        r = conn.execute("SELECT * FROM shop_hours WHERE merchant_id = ?", (merchant_id,)).fetchone()
    except sqlite3.OperationalError:                        # the table does not exist yet: nobody has reported any hours
        r = None
    if r is not None:
        return Hours(to_minutes(r["open_time"]), to_minutes(r["close_time"]), tuple(d for d in r["closed_days"].split(",") if d), r["source"])
    m = conn.execute("SELECT segment FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    return default_for(m["segment"] if m else None)


def set_hours(conn: sqlite3.Connection, clock, merchant_id: str, open_time: str, close_time: str, closed_days: tuple[str, ...] = (), *,
              ledger=None, source: str = REPORTED) -> Hours:
    """Record the hours a merchant (or a person on their behalf) reported. Validated: real times, close after open, known weekday names."""
    o, c = to_minutes(open_time), to_minutes(close_time)
    if c <= o:
        raise ValueError("close must be after open (a shop open past midnight is not modelled)")
    bad = [d for d in closed_days if d not in WEEKDAYS]
    if bad:
        raise ValueError(f"unknown weekday {bad[0]!r}; use {', '.join(WEEKDAYS)}")
    if source not in (REPORTED, ASSUMED):
        raise ValueError("source is REPORTED or ASSUMED")
    if conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone() is None:
        raise KeyError(merchant_id)
    conn.execute(SCHEMA)
    with write_txn(conn):
        conn.execute("INSERT INTO shop_hours(merchant_id, open_time, close_time, closed_days, source, updated_at) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(merchant_id) DO UPDATE SET open_time = excluded.open_time, close_time = excluded.close_time, closed_days = excluded.closed_days, "
                     "source = excluded.source, updated_at = excluded.updated_at",
                     (merchant_id, open_time, close_time, ",".join(closed_days), source, clock.now_iso()))
        if ledger is not None:
            ledger.append(kind="STATE", actor="SYSTEM", payload={"shop_hours": merchant_id, "open": open_time, "close": close_time, "closed_days": list(closed_days), "source": source})
    return get_hours(conn, merchant_id)
