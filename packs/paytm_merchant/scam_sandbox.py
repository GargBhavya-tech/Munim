"""The scam sandbox: put a customer's "I paid" claim into a throwaway copy of the merchant's books and see what breaks.

Scam Shield (``scam_shield.py``) decides whether a payment screen is real by matching the merchant's own transactions. This is the step before it: it answers "is this a
real person with a real problem, or a scam?" without touching a single real record. It

  1. copies only what the question needs (this merchant's recent payments, and which merchant owns each payment reference) into an in-memory database,
  2. places the claimed payment in that copy as if it were true,
  3. asks the copy what it now looks like (a reference that someone else already owns, a reference that belongs to a different amount, a day far above this merchant's normal,
     an amount far above their usual ticket, a time the shop was shut),
  4. throws the copy away. Nothing is ever written to the real database, and the report says so.

The VERDICT is code's: REAL (a real successful payment of exactly that amount, and that reference if one was given, inside the window), LIKELY_SCAM (a hard contradiction), or
CANNOT_TELL (soft signals only: a person looks, and the merchant is asked, never accused). No number is estimated: every figure in a signal is read from a record.
The thresholds are [ASSUMPTION]s. The wording of signals is for people to read; nothing here is said to the merchant.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from core.engine.clock import SimClock, parse_iso
from field.shop_hours import get_hours

REFERENCE = re.compile(r"^UPI\d{12}$")                  # the shape of a reference in the simulated payment system
WINDOW_MIN = 10                                         # [ASSUMPTION] same as Scam Shield: claim time plus or minus ten minutes
NO_TIME_WINDOW_MIN = 15                                 # [ASSUMPTION] no time given: the last fifteen minutes
OUTLIER_TICKET_X = 10                                   # [ASSUMPTION] an amount ten times their median ticket is unusual
DAY_STRETCH_X = 3                                       # [ASSUMPTION] a day three times their normal volume is unusual
LOOKBACK_DAYS = 30
MIN_TICKETS = 3

HARD, SOFT = "hard", "soft"
REAL, LIKELY_SCAM, CANNOT_TELL = "REAL", "LIKELY_SCAM", "CANNOT_TELL"


@dataclass(frozen=True)
class Claim:
    amount_paise: int
    reference: str | None = None
    claimed_at: str | None = None                       # ISO time the customer says they paid


@dataclass(frozen=True)
class Signal:
    code: str
    severity: str                                       # hard | soft
    text: str


@dataclass
class SandboxReport:
    merchant_id: str
    verdict: str
    signals: list[Signal] = field(default_factory=list)
    copied: dict = field(default_factory=dict)          # what was copied into the sandbox
    discarded: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _build(conn: sqlite3.Connection, merchant_id: str, since: str) -> sqlite3.Connection:
    sb = sqlite3.connect(":memory:")
    sb.row_factory = sqlite3.Row
    sb.execute("CREATE TABLE payments (payment_id TEXT PRIMARY KEY, merchant_id TEXT, amount_paise INTEGER, status TEXT, ts TEXT, reference TEXT)")
    sb.execute("CREATE TABLE owners (reference TEXT, merchant_id TEXT)")
    sb.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?)",
                   [tuple(r) for r in conn.execute("SELECT payment_id, merchant_id, amount_paise, status, ts, reference FROM payments WHERE merchant_id = ? AND ts >= ?",
                                                   (merchant_id, since))])
    sb.executemany("INSERT INTO owners VALUES (?,?)", [tuple(r) for r in conn.execute("SELECT reference, merchant_id FROM payments WHERE reference IS NOT NULL")])
    return sb


def check_claim(conn: sqlite3.Connection, clock: SimClock, merchant_id: str, claim: Claim) -> SandboxReport:
    if claim.amount_paise <= 0:
        raise ValueError("a claimed amount is a positive number of paise")
    m = conn.execute("SELECT avg_daily_volume_paise FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    if m is None:
        raise KeyError(merchant_id)
    now = clock.now()
    at = parse_iso(claim.claimed_at) if claim.claimed_at else now
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    sb = _build(conn, merchant_id, since)
    try:
        copied = {"payments": sb.execute("SELECT COUNT(*) FROM payments").fetchone()[0], "references": sb.execute("SELECT COUNT(*) FROM owners").fetchone()[0]}
        signals: list[Signal] = []
        lo, hi = ((at - timedelta(minutes=WINDOW_MIN), at + timedelta(minutes=WINDOW_MIN)) if claim.claimed_at else (now - timedelta(minutes=NO_TIME_WINDOW_MIN), now))
        in_window = [r for r in sb.execute("SELECT * FROM payments WHERE amount_paise = ? ORDER BY ts", (claim.amount_paise,)) if lo <= parse_iso(r["ts"]) <= hi]
        exact = [r for r in in_window if r["status"] == "SUCCESS" and (claim.reference is None or r["reference"] == claim.reference)]
        pending = [r for r in in_window if r["status"] == "PENDING"]

        if claim.reference is not None:
            if not REFERENCE.match(claim.reference):
                signals.append(Signal("REFERENCE_FORMAT", HARD, "The reference is not shaped like a payment reference"))
            owners = {r["merchant_id"] for r in sb.execute("SELECT merchant_id FROM owners WHERE reference = ?", (claim.reference,))}
            if owners - {merchant_id}:
                signals.append(Signal("REFERENCE_OTHER_MERCHANT", HARD, "This reference belongs to a payment made to a different merchant"))
            same = sb.execute("SELECT amount_paise FROM payments WHERE reference = ? AND merchant_id = ?", (claim.reference, merchant_id)).fetchone()
            if same is not None and same["amount_paise"] != claim.amount_paise:
                signals.append(Signal("REFERENCE_OTHER_AMOUNT", HARD, f"This reference is on file for a payment of {same['amount_paise']} paise, not the claimed {claim.amount_paise}"))

        # place the claim in the sandbox as if it were true, then ask what the copy looks like
        sb.execute("INSERT INTO payments VALUES ('CLAIM', ?, ?, 'SUCCESS', ?, ?)", (merchant_id, claim.amount_paise, at.isoformat(), claim.reference))
        tickets = [r["amount_paise"] for r in sb.execute("SELECT amount_paise FROM payments WHERE status = 'SUCCESS' AND payment_id != 'CLAIM'")]
        if len(tickets) >= MIN_TICKETS:
            median = int(statistics.median(tickets))
            if median > 0 and claim.amount_paise >= OUTLIER_TICKET_X * median:
                signals.append(Signal("AMOUNT_OUTLIER", SOFT, f"The amount is at least {OUTLIER_TICKET_X} times this merchant's usual payment ({median} paise)"))
        day = at.date().isoformat()
        total = sb.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE status = 'SUCCESS' AND ts LIKE ?", (f"{day}%",)).fetchone()[0]
        normal = int(m["avg_daily_volume_paise"] or 0)
        if normal > 0 and total >= DAY_STRETCH_X * normal:
            signals.append(Signal("DAY_FAR_ABOVE_NORMAL", SOFT, f"With this payment the day would total {total} paise, at least {DAY_STRETCH_X} times a normal day ({normal} paise)"))
        if not get_hours(conn, merchant_id).is_open(at):
            signals.append(Signal("OUTSIDE_SHOP_HOURS", SOFT, "The shop was not open at the time claimed"))
        if pending and not exact:
            signals.append(Signal("ONLY_PENDING", SOFT, "A payment of this amount is still pending, not yet successful"))
    finally:
        sb.close()                                      # the sandbox is gone; nothing was ever written to the real database

    if exact and not any(s.severity == HARD for s in signals):
        verdict = REAL
    elif any(s.severity == HARD for s in signals):
        verdict = LIKELY_SCAM
    else:
        verdict = CANNOT_TELL
    if verdict == CANNOT_TELL and not exact:
        signals.append(Signal("NO_MATCHING_PAYMENT", SOFT, "No successful payment of this amount was found in the window"))
    return SandboxReport(merchant_id, verdict, signals, copied)
