"""A rep's incentive follows merchant activity, not signatures.

A signup that never transacts is not a win, so most of the incentive for a lead is earned over the merchant's first months, not on the day they say yes:

  * a lead counts for a rep only when the rep's visit was VERIFIED (merchant-confirmed, geotagged near the shop, with a photo: ``field/visits``), its outcome was CONVERTED, and
    the lead was then won (``leads.matched_merchant_id`` is set). Nothing else is attributed;
  * ``ACTIVATION_SHARE`` of the pool is earned when the merchant's FIRST SETTLEMENT reaches the bank (sale won = money arrived, not "yes");
  * the rest is earned month by month for ``ACTIVITY_MONTHS`` months after that, each month in proportion to how much of ``MONTHLY_TARGET_PAISE`` the merchant actually processed
    (capped at the whole month's share). A merchant who stops transacting stops earning for the rep.

All money is integer paise computed from system rows, with integer arithmetic. The pool, the shares and the target are [ASSUMPTION]s on synthetic data and are returned with the
statement. This is a STATEMENT of what is earned and what is still pending; it pays nothing, and no coaching or ranking data is read.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from core.engine.clock import SimClock, iso, parse_iso
from field.visits import CONFIRMED

POOL_PAISE = 200_000                    # [ASSUMPTION] 2,000 rupees per activated merchant
ACTIVATION_SHARE_PCT = 30               # earned when the first settlement reaches the bank
ACTIVITY_MONTHS = 3                     # the remaining share is earned over this many months
MONTHLY_TARGET_PAISE = 5_000_000        # [ASSUMPTION] 50,000 rupees a month processed earns a month's share in full
MONTH_DAYS = 30
NOT_COUNTED, ACTIVATED = "NOT_COUNTED_YET", "ACTIVATED"


@dataclass
class MerchantLine:
    lead_id: str
    merchant_id: str | None
    merchant_name: str | None
    visit_id: str
    status: str
    first_settlement_at: str | None
    earned_paise: int
    pending_paise: int
    months: list[dict] = field(default_factory=list)
    note: str = ""


@dataclass
class Statement:
    rep_id: str
    lines: list[MerchantLine] = field(default_factory=list)
    earned_paise: int = 0
    pending_paise: int = 0
    assumptions: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _converted_visits(conn: sqlite3.Connection, rep_id: str) -> list[sqlite3.Row]:
    try:
        rows = conn.execute("SELECT * FROM field_visits WHERE rep_id = ? AND status = ? AND geo_ok = 1 AND photo_ref IS NOT NULL ORDER BY visit_id", (rep_id, CONFIRMED)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r for r in rows if json.loads(r["fields_json"]).get("outcome") == "CONVERTED"]


def _volume(conn: sqlite3.Connection, merchant_id: str, start, end) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE merchant_id = ? AND status = 'SUCCESS' AND ts >= ? AND ts < ?",
                        (merchant_id, iso(start), iso(end))).fetchone()[0]


def statement(conn: sqlite3.Connection, clock: SimClock, rep_id: str) -> Statement:
    if conn.execute("SELECT 1 FROM reps WHERE rep_id = ?", (rep_id,)).fetchone() is None:
        raise KeyError(rep_id)
    now = clock.now()
    activation = POOL_PAISE * ACTIVATION_SHARE_PCT // 100
    month_share = (POOL_PAISE - activation) // ACTIVITY_MONTHS
    out = Statement(rep_id, assumptions={"pool_paise": POOL_PAISE, "activation_share_pct": ACTIVATION_SHARE_PCT, "activity_months": ACTIVITY_MONTHS,
                                         "monthly_target_paise": MONTHLY_TARGET_PAISE, "month_share_paise": month_share})
    seen = set()
    for v in _converted_visits(conn, rep_id):
        if v["lead_id"] in seen:
            continue
        seen.add(v["lead_id"])
        lead = conn.execute("SELECT name, matched_merchant_id FROM leads WHERE lead_id = ?", (v["lead_id"],)).fetchone()
        mid = lead["matched_merchant_id"] if lead else None
        if mid is None:
            out.lines.append(MerchantLine(v["lead_id"], None, lead["name"] if lead else None, v["visit_id"], NOT_COUNTED, None, 0, POOL_PAISE,
                                          note="The visit converted, but the lead has not been won yet: nothing counts until they sign up"))
            continue
        name = conn.execute("SELECT name FROM merchants WHERE merchant_id = ?", (mid,)).fetchone()
        first = conn.execute("SELECT MIN(settled_at) FROM settlements WHERE merchant_id = ? AND status = 'SETTLED' AND settled_at IS NOT NULL", (mid,)).fetchone()[0]
        if first is None:
            out.lines.append(MerchantLine(v["lead_id"], mid, name["name"] if name else None, v["visit_id"], NOT_COUNTED, None, 0, POOL_PAISE,
                                          note="Signed up, but no settlement has reached the bank yet: this is not a win yet"))
            continue
        start = parse_iso(first)
        earned, months = activation, []
        for k in range(ACTIVITY_MONTHS):
            lo, hi = start + timedelta(days=MONTH_DAYS * k), start + timedelta(days=MONTH_DAYS * (k + 1))
            if hi > now:
                months.append({"month": k + 1, "status": "NOT_YET", "volume_paise": None, "earned_paise": 0, "share_paise": month_share})
                continue
            vol = _volume(conn, mid, lo, hi)
            got = month_share * min(vol, MONTHLY_TARGET_PAISE) // MONTHLY_TARGET_PAISE
            earned += got
            months.append({"month": k + 1, "status": "COMPLETE", "volume_paise": vol, "earned_paise": got, "share_paise": month_share})
        out.lines.append(MerchantLine(v["lead_id"], mid, name["name"] if name else None, v["visit_id"], ACTIVATED, first, earned, POOL_PAISE - earned, months,
                                      "Counted: the first settlement reached the bank. The rest follows the merchant's activity"))
    out.earned_paise = sum(l.earned_paise for l in out.lines)
    out.pending_paise = sum(l.pending_paise for l in out.lines)
    return out
