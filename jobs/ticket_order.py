"""The human queue order (Bible section 18, A31).

1. lane: fraud/safety -> money stuck -> service down -> information
2. EARLIEST DEADLINE FIRST inside a lane (regulatory refund deadline, SLA, handoff deadline). Deadlines are HARD limits.
3. only when two deadlines are exactly equal does the value score break the tie.

Ageing: a ticket with no explicit deadline gets an implied one (created + the lane's default SLA), so the longer it waits the
earlier its deadline looms; ageing also adds to the value score. Every ticket carries a human-readable reason.
Human overrides are logged in ``queue_overrides``. This module only orders: it never contacts a merchant or acts.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.engine.clock import SimClock, parse_iso

LANES = ("FRAUD_SAFETY", "MONEY_STUCK", "SERVICE_DOWN", "INFORMATION")
LANE_LABEL = {"FRAUD_SAFETY": "fraud/safety", "MONEY_STUCK": "money stuck", "SERVICE_DOWN": "service down", "INFORMATION": "information"}
DEFAULT_SLA = {"FRAUD_SAFETY": timedelta(hours=2), "MONEY_STUCK": timedelta(hours=24), "SERVICE_DOWN": timedelta(hours=24), "INFORMATION": timedelta(hours=72)}
_TYPE_LANE = {"FRAUD": "FRAUD_SAFETY", "RISK": "FRAUD_SAFETY", "SAFETY": "FRAUD_SAFETY", "SCAM": "FRAUD_SAFETY",
              "SETTLEMENT": "MONEY_STUCK", "REFUND": "MONEY_STUCK", "DISPUTE": "MONEY_STUCK", "PAYMENT": "MONEY_STUCK",
              "DEVICE": "SERVICE_DOWN", "SERVICE": "SERVICE_DOWN"}

PAISE_PER_FRUSTRATION_POINT = 50_000      # Rs 500 of "value" per frustration point (0..5)
PAISE_PER_REPEAT_CONTACT = 50_000
PAISE_PER_EXTRA_MERCHANT = 100_000
PAISE_PER_WAITING_HOUR = 10_000           # ageing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_overrides (
  override_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, position INTEGER NOT NULL,
  by_user TEXT NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL, from_position INTEGER, active INTEGER NOT NULL DEFAULT 1
);
"""


def lane_for_type(ticket_type: str | None) -> str:
    return _TYPE_LANE.get((ticket_type or "").upper(), "INFORMATION")


@dataclass(frozen=True)
class QueueItem:
    ticket_id: str
    lane: str
    created_at: datetime
    deadline: datetime | None = None          # earliest of the regulatory / SLA / handoff deadlines, when one exists
    money_at_risk_paise: int = 0
    daily_volume_paise: int = 0               # the merchant's usual daily volume
    down_since: datetime | None = None        # set while the merchant is down
    frustration: int = 0                      # 0..5
    contacts: int = 1                         # how many times the merchant has contacted us about this
    merchants_affected: int = 1


@dataclass(frozen=True)
class RankedTicket:
    position: int
    ticket_id: str
    lane: str
    effective_deadline: datetime
    value_paise: int
    reason: str
    overridden_by: str | None = None


def _rupees(paise: int) -> str:
    """Indian digit grouping: 1240000 paise -> 'Rs 12,400' with the rupee sign."""
    r = paise // 100
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return "₹" + s


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _hours(td: timedelta) -> str:
    h = abs(td.total_seconds()) / 3600
    return f"{round(h)} h" if h >= 1 else f"{round(h * 60)} min"


def effective_deadline(item: QueueItem) -> datetime:
    return item.deadline if item.deadline is not None else item.created_at + DEFAULT_SLA[item.lane]


def sales_lost_paise(item: QueueItem, now: datetime) -> int:
    if item.down_since is None or item.daily_volume_paise <= 0:
        return 0
    hours = max(0.0, (now - item.down_since).total_seconds() / 3600)
    return int(item.daily_volume_paise * min(hours, 24 * 30) / 24)


def value_score(item: QueueItem, now: datetime) -> int:
    waited_h = max(0, int((now - item.created_at).total_seconds() // 3600))
    return (item.money_at_risk_paise + sales_lost_paise(item, now)
            + item.frustration * PAISE_PER_FRUSTRATION_POINT
            + max(0, item.contacts - 1) * PAISE_PER_REPEAT_CONTACT
            + max(0, item.merchants_affected - 1) * PAISE_PER_EXTRA_MERCHANT
            + waited_h * PAISE_PER_WAITING_HOUR)


def reason_for(item: QueueItem, now: datetime) -> str:
    dl = effective_deadline(item)
    left = dl - now
    word = "deadline" if item.deadline is not None else "SLA"
    parts = [f"{word} breached {_hours(-left)} ago" if left < timedelta(0) else f"{word} in {_hours(left)}"]
    if item.money_at_risk_paise:
        parts.append(_rupees(item.money_at_risk_paise))
    lost = sales_lost_paise(item, now)
    if lost:
        parts.append(f"{_rupees(lost)} sales lost")
    if item.contacts >= 2:
        parts.append(f"{_ordinal(item.contacts)} contact")
    if item.merchants_affected > 1:
        parts.append(f"{item.merchants_affected} merchants")
    return " · ".join(parts)


def order_queue(items: list[QueueItem], now: datetime) -> list[RankedTicket]:
    """Pure ordering. Key: (lane, effective deadline, -value, ticket_id). Value is reached only when deadlines are equal."""
    for it in items:
        if it.lane not in LANES:
            raise ValueError(f"unknown lane {it.lane!r}")
    scored = [(LANES.index(it.lane), effective_deadline(it), -value_score(it, now), it.ticket_id, it) for it in items]
    scored.sort(key=lambda t: t[:4])
    return [RankedTicket(i + 1, it.ticket_id, it.lane, dl, -negv, reason_for(it, now)) for i, (_, dl, negv, _, it) in enumerate(scored)]


class OrderedQueue:
    """The queue with a persistent, logged override trail."""

    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock
        conn.executescript(_SCHEMA)

    def order(self, items: list[QueueItem]) -> list[RankedTicket]:
        ranked = order_queue(items, self.clock.now())
        live = {r.ticket_id for r in ranked}
        rows = self.conn.execute("SELECT ticket_id, position, by_user, reason FROM queue_overrides WHERE active=1 ORDER BY override_id").fetchall()
        seq = [r for r in ranked]
        pinned: dict[str, str] = {}
        for tid, pos, by, why in rows:
            if tid not in live:
                continue
            cur = next(r for r in seq if r.ticket_id == tid)
            seq.remove(cur)
            seq.insert(min(max(pos, 1), len(seq) + 1) - 1, cur)
            pinned[tid] = f"{by}: {why}"
        out = []
        for i, r in enumerate(seq):
            if r.ticket_id in pinned:
                out.append(RankedTicket(i + 1, r.ticket_id, r.lane, r.effective_deadline, r.value_paise,
                                        f"{r.reason} · moved by {pinned[r.ticket_id]}", pinned[r.ticket_id].split(":")[0]))
            else:
                out.append(RankedTicket(i + 1, r.ticket_id, r.lane, r.effective_deadline, r.value_paise, r.reason))
        return out

    def override(self, items: list[QueueItem], ticket_id: str, position: int, *, by: str, reason: str) -> None:
        """A human moves a ticket to a 1-based position. Always logged; a blank actor or reason is refused."""
        if not by.strip() or not reason.strip():
            raise ValueError("an override needs who and why")
        before = {r.ticket_id: r.position for r in self.order(items)}
        if ticket_id not in before:
            raise KeyError(ticket_id)
        self.conn.execute("INSERT INTO queue_overrides(ticket_id, position, by_user, reason, ts, from_position) VALUES (?,?,?,?,?,?)",
                          (ticket_id, position, by.strip(), reason.strip(), self.clock.now_iso(), before[ticket_id]))
        self.conn.commit()

    def override_log(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM queue_overrides ORDER BY override_id")]
