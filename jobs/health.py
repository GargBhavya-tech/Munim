"""Merchant health, usage drops and predictive faults (Bible section 20).

A plain program over the analytics tables (payments, settlements, devices, tickets, plans). It EMITS EVENTS
(``USAGE_DROP``, ``DEVICE_DEGRADING``) and writes its own rows (``merchant_health``, ``retention_queue``). It never messages a
merchant, never calls a mock and never acts: what to do about an event is the engine's and the humans' business.

Real drop versus noise:
* against the merchant's OWN normal: the median of the last four same weekdays, divided and re-multiplied by the
  festival / month-end ``adjustments`` the caller passes;
* against NEIGHBOURS in the same pincode: if the whole area dipped, the cause is external (rain, a bandh) and NO
  per-merchant event is produced;
* persistence: below the threshold on each of the last 3 completed days.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Mapping

from core.engine.clock import SimClock, parse_iso
from core.engine.events import append_event

DROP_RATIO = 0.7          # below 70% of the merchant's own normal counts as "below threshold"
PERSIST_DAYS = 3
AREA_DIP_RATIO = 0.8      # the neighbours' median below this means the area dipped
AREA_EXPLAINS = 0.7       # the merchant is explained by the area when own/area >= this
MIN_BASELINE_DAYS = 3
MIN_NEIGHBOURS = 3
OFFLINE_HOURS = 6
ZERO_DAYS = 7
SETTLEMENT_FAIL_DAYS = 7
RETENTION_SCORE = 50
RETENTION_FALL = 15

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_telemetry (
  device_id TEXT NOT NULL, ts TEXT NOT NULL, battery_pct INTEGER, signal_pct INTEGER, heartbeat_gap_s INTEGER,
  PRIMARY KEY (device_id, ts)
);
CREATE TABLE IF NOT EXISTS merchant_health (
  merchant_id TEXT NOT NULL, as_of TEXT NOT NULL, score INTEGER NOT NULL, components_json TEXT NOT NULL, diagnosis TEXT,
  PRIMARY KEY (merchant_id, as_of)
);
CREATE TABLE IF NOT EXISTS retention_queue (
  merchant_id TEXT NOT NULL, as_of TEXT NOT NULL, score INTEGER NOT NULL, reason TEXT NOT NULL, PRIMARY KEY (merchant_id, as_of)
);
"""

DEVICE_OFFLINE = "DEVICE_OFFLINE"
SETTLEMENT_FAILURE = "SETTLEMENT_FAILURE"
COMPETITOR_SWITCH = "COMPETITOR_SWITCH"
AREA_WIDE = "AREA_WIDE_DIP"
AREA_OUTAGE = "AREA_OUTAGE"
ZERO_ACTIVITY = "ZERO_ACTIVITY"
UNEXPLAINED_DROP = "UNEXPLAINED_DROP"

ACTION_FOR = {DEVICE_OFFLINE: "SERVICE_TICKET", SETTLEMENT_FAILURE: "SUPPORT_HANDOFF", COMPETITOR_SWITCH: "RETENTION_HANDOFF",
              AREA_WIDE: "NO_ACTION", AREA_OUTAGE: "NO_ACTION", ZERO_ACTIVITY: "CHECK_IN_CALL", UNEXPLAINED_DROP: "INSIGHTS_OFFER"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def record_telemetry(conn: sqlite3.Connection, device_id: str, ts: str, *, battery_pct: int | None, signal_pct: int | None,
                     heartbeat_gap_s: int | None) -> None:
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO device_telemetry VALUES (?,?,?,?,?)", (device_id, ts, battery_pct, signal_pct, heartbeat_gap_s))
    conn.commit()


@dataclass(frozen=True)
class Finding:
    merchant_id: str
    diagnosis: str
    action: str
    event_type: str | None            # None = nothing was emitted (no action, or no event exists for it)
    detail: dict = field(default_factory=dict)


@dataclass
class HealthReport:
    as_of: str
    findings: list[Finding] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)
    retention: list[str] = field(default_factory=list)

    def for_merchant(self, mid: str) -> list[Finding]:
        return [f for f in self.findings if f.merchant_id == mid]


class HealthJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, *, adjustments: Mapping[str, float] | None = None,
                 competitor_signals: set[str] | None = None, sentiments: Mapping[str, float] | None = None):
        """``adjustments``: ISO date -> expected volume multiplier vs a normal day (month-end 1.15, a festival 1.4, a bandh 0.4).
        ``competitor_signals``: merchant ids with a competitor QR seen (also read from profile_facts ``competitor_qr_seen``).
        ``sentiments``: merchant id -> recent sentiment in [-1, 1]."""
        self.conn, self.clock = conn, clock
        self.adjustments = dict(adjustments or {})
        self.competitor_signals = set(competitor_signals or ())
        self.sentiments = dict(sentiments or {})
        ensure_schema(conn)

    # ------------------------------------------------------------ data
    def _load_volumes(self, since: date) -> dict[str, dict[str, tuple[int, int]]]:
        out: dict[str, dict[str, tuple[int, int]]] = {}
        for r in self.conn.execute("SELECT merchant_id, substr(ts,1,10) d, SUM(amount_paise), COUNT(*) FROM payments "
                                   "WHERE status='SUCCESS' AND ts >= ? GROUP BY 1,2", (since.isoformat(),)):
            out.setdefault(r[0], {})[r[1]] = (r[2], r[3])
        return out

    def _ratio(self, vols: dict[str, tuple[int, int]], day: date, idx: int) -> float | None:
        """Own volume (idx 0) or transaction count (idx 1) on ``day`` against the median of the last four same weekdays."""
        if not vols:
            return None
        first = min(vols)
        base = []
        for k in range(1, 5):
            b = day - timedelta(days=7 * k)
            if b.isoformat() < first:
                continue            # before the merchant's first payment: no baseline, not a zero
            base.append(vols.get(b.isoformat(), (0, 0))[idx] / self.adjustments.get(b.isoformat(), 1.0))
        if len(base) < MIN_BASELINE_DAYS:
            return None
        expected = statistics.median(base) * self.adjustments.get(day.isoformat(), 1.0)
        if expected <= 0:
            return None
        return vols.get(day.isoformat(), (0, 0))[idx] / expected

    # ------------------------------------------------------------ the run
    def run(self, merchant_ids: list[str] | None = None) -> HealthReport:
        now = self.clock.now()
        today = now.date()
        report = HealthReport(as_of=today.isoformat())
        vols = self._load_volumes(today - timedelta(days=40))
        merchants = {r["merchant_id"]: dict(r) for r in self.conn.execute("SELECT * FROM merchants WHERE status='ACTIVE' ORDER BY merchant_id")}
        by_pin: dict[str, list[str]] = {}
        for m in merchants.values():
            by_pin.setdefault(m["pincode"], []).append(m["merchant_id"])
        days = [today - timedelta(days=i) for i in range(1, PERSIST_DAYS + 1)]       # completed days only
        targets = [m for m in (merchant_ids if merchant_ids is not None else sorted(merchants)) if m in merchants]
        offline_by_pin = self._offline_share(merchants, by_pin)

        for mid in targets:
            f = self._diagnose(mid, merchants[mid], vols, by_pin, days, today, offline_by_pin)
            if f is not None:
                report.findings.append(f)
            report.findings.extend(self._degrading(mid, today))
            score, comps = self._score(mid, vols, days, report.for_merchant(mid))
            report.scores[mid] = score
            self._store_score(mid, today.isoformat(), score, comps, f.diagnosis if f else None, report)
        self.conn.commit()
        return report

    # ------------------------------------------------------------ diagnosis
    def _device_state(self, mid: str) -> tuple[bool, str | None]:
        """(every device is offline or silent for OFFLINE_HOURS+ and no payment has arrived since, last heartbeat)."""
        now = self.clock.now()
        rows = self.conn.execute("SELECT * FROM devices WHERE merchant_id=? ORDER BY device_id", (mid,)).fetchall()
        if not rows:
            return False, None
        quiet = timedelta(hours=OFFLINE_HOURS)
        for d in rows:
            hb = d["last_heartbeat"]
            if d["status"] == "ONLINE" and (hb is None or now - parse_iso(hb) < quiet):
                return False, hb                      # at least one device is healthy
        hbs = [d["last_heartbeat"] for d in rows if d["last_heartbeat"]]
        hb = max(hbs) if hbs else None
        if hb is not None and now - parse_iso(hb) < quiet:
            return False, hb                          # went quiet only recently
        since = hb or (now - timedelta(hours=24)).isoformat()
        n = self.conn.execute("SELECT COUNT(*) FROM payments WHERE merchant_id=? AND status='SUCCESS' AND ts > ?", (mid, since)).fetchone()[0]
        return n == 0, hb

    def _offline_share(self, merchants, by_pin) -> dict[str, float]:
        """Per pincode: the share of device-owning merchants whose device is not online. A pincode-wide outage is an incident, not N tickets."""
        share = {}
        for pin, mids in by_pin.items():
            ph = ",".join("?" * len(mids))
            row = self.conn.execute(f"SELECT COUNT(DISTINCT merchant_id), COUNT(DISTINCT CASE WHEN status != 'ONLINE' THEN merchant_id END) "
                                    f"FROM devices WHERE merchant_id IN ({ph})", mids).fetchone()
            if row[0] >= MIN_NEIGHBOURS + 1:
                share[pin] = row[1] / row[0]
        return share

    def _has_competitor_signal(self, mid: str) -> bool:
        if mid in self.competitor_signals:
            return True
        r = self.conn.execute("SELECT 1 FROM profile_facts WHERE merchant_id=? AND key='competitor_qr_seen' AND valid_to IS NULL "
                              "AND LOWER(COALESCE(value,'')) NOT IN ('','0','false','no') LIMIT 1", (mid,)).fetchone()
        return r is not None

    def _settlement_failures(self, mid: str, today: date) -> int:
        cutoff = (today - timedelta(days=SETTLEMENT_FAIL_DAYS)).isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM settlements WHERE merchant_id=? AND status='FAILED' AND expected_at >= ?", (mid, cutoff)).fetchone()[0]

    def _diagnose(self, mid, merchant, vols, by_pin, days, today, offline_by_pin) -> Finding | None:
        mv = vols.get(mid, {})
        own = [self._ratio(mv, d, 0) for d in days]
        own_cnt = [self._ratio(mv, d, 1) for d in days]
        pin = merchant["pincode"]
        offline, hb = self._device_state(mid)

        # 1. device offline and no sales since: hardware, network or power. Does not wait for 3 days.
        if offline:
            if offline_by_pin.get(pin, 0.0) >= 0.5:
                return self._finding(mid, AREA_OUTAGE, today, {"pincode": pin, "offline_share": round(offline_by_pin[pin], 2)}, emit=False)
            return self._finding(mid, DEVICE_OFFLINE, today, {"last_heartbeat": hb, "sales_since": 0})

        # 2. settlement failures: a bank or account problem
        failures = self._settlement_failures(mid, today)
        persistent = all(r is not None and r < DROP_RATIO for r in own)
        if failures and persistent:
            return self._finding(mid, SETTLEMENT_FAILURE, today, {"failed_settlements": failures, "ratios": [round(r, 3) for r in own]})

        # 2b. nothing at all for 7+ days (and not because the whole area is quiet): closed or relocated
        zero = self._zero_days(mv, today)
        if zero >= ZERO_DAYS and not self._area_down(mid, by_pin[pin], vols, days, own)[0]:
            return self._finding(mid, ZERO_ACTIVITY, today, {"zero_days": zero})
        if not persistent:
            return None

        # 3. persistent drop: the area first, then why
        area_down, area_med = self._area_down(mid, by_pin[pin], vols, days, own)
        detail = {"ratios": [round(r, 3) for r in own], "count_ratios": [round(r, 3) if r is not None else None for r in own_cnt],
                  "area_median_ratio": round(area_med, 3) if area_med is not None else None, "persistent_days": PERSIST_DAYS}
        if area_down:
            return self._finding(mid, AREA_WIDE, today, detail, emit=False)
        if self._has_competitor_signal(mid):
            return self._finding(mid, COMPETITOR_SWITCH, today, detail)
        return self._finding(mid, UNEXPLAINED_DROP, today, detail)

    def _zero_days(self, mv, today) -> int:
        if not mv:
            return 0
        n = 0
        for i in range(1, ZERO_DAYS + 1):
            d = (today - timedelta(days=i)).isoformat()
            if d < min(mv):
                break
            if mv.get(d, (0, 0))[0] == 0:
                n += 1
            else:
                break
        return n

    def _area_down(self, mid, pin_mids, vols, days, own) -> tuple[bool, float | None]:
        neigh = [m for m in pin_mids if m != mid]
        meds = []
        for d in days:
            rs = [r for r in (self._ratio(vols.get(m, {}), d, 0) for m in neigh) if r is not None]
            if len(rs) >= MIN_NEIGHBOURS:
                meds.append(statistics.median(rs))
        if not meds:
            return False, None
        area_med = statistics.median(meds)
        dipped = sum(1 for m in meds if m < AREA_DIP_RATIO) >= 2
        own_vals = [r for r in own if r is not None]
        own_med = statistics.median(own_vals) if own_vals else 0.0
        explained = area_med > 0 and own_med / area_med >= AREA_EXPLAINS if area_med else True
        return (dipped and explained), area_med

    def _finding(self, mid, diagnosis, today, detail, emit=True) -> Finding:
        action = ACTION_FOR[diagnosis]
        etype = None
        if emit and action != "NO_ACTION":
            etype = "USAGE_DROP"
            append_event(self.conn, event_id=f"usage_drop:{mid}:{today.isoformat()}:{diagnosis}", type=etype, merchant_id=mid,
                         payload={"diagnosis": diagnosis, "action": action, **detail}, ts_sim=self.clock.now_iso())
        return Finding(mid, diagnosis, action, etype, detail)

    # ------------------------------------------------------------ predictive faults
    def _degrading(self, mid: str, today: date) -> list[Finding]:
        out = []
        for d in self.conn.execute("SELECT device_id, status FROM devices WHERE merchant_id=? ORDER BY device_id", (mid,)).fetchall():
            if d["status"] != "ONLINE":
                continue              # already failed: that is a fault, not a prediction
            cutoff = (self.clock.now() - timedelta(days=14)).isoformat()
            s = self.conn.execute("SELECT * FROM device_telemetry WHERE device_id=? AND ts >= ? ORDER BY ts", (d["device_id"], cutoff)).fetchall()
            if len(s) < 4:
                continue
            signals = []
            b = [x["battery_pct"] for x in s if x["battery_pct"] is not None]
            if len(b) >= 4 and ((b[0] - b[-1] >= 15 and b[-1] <= 30) or (b[-1] <= 15 and b[0] - b[-1] >= 5)):
                signals.append("BATTERY_FALLING")
            g = [x["signal_pct"] for x in s if x["signal_pct"] is not None]
            if len(g) >= 4 and g[0] - g[-1] >= 20 and g[-1] <= 40:
                signals.append("SIGNAL_FALLING")
            h = [x["heartbeat_gap_s"] for x in s if x["heartbeat_gap_s"] is not None]
            if len(h) >= 6 and statistics.mean(h[-3:]) >= 2 * max(statistics.mean(h[:3]), 1) and statistics.mean(h[-3:]) >= 600:
                signals.append("HEARTBEAT_GAPS_GROWING")
            if signals:
                payload = {"device_id": d["device_id"], "signals": signals, "battery_pct": b[-1] if b else None, "signal_pct": g[-1] if g else None}
                append_event(self.conn, event_id=f"degrading:{d['device_id']}:{today.isoformat()}", type="DEVICE_DEGRADING", merchant_id=mid,
                             payload=payload, ts_sim=self.clock.now_iso())
                out.append(Finding(mid, "DEVICE_DEGRADING", "FIX_BEFORE_FAILURE", "DEVICE_DEGRADING", payload))
        return out

    # ------------------------------------------------------------ health score
    def _score(self, mid, vols, days, findings) -> tuple[int, dict]:
        ratios = [r for r in (self._ratio(vols.get(mid, {}), d, 0) for d in days) if r is not None]
        usage = round(40 * min(statistics.mean(ratios), 1.0)) if ratios else 30
        devs = self.conn.execute("SELECT status FROM devices WHERE merchant_id=?", (mid,)).fetchall()
        if not devs:
            device = 10
        elif any(d["status"] == "ONLINE" for d in devs):
            device = 10 if any(f.diagnosis == "DEVICE_DEGRADING" for f in findings) else 20
        else:
            device = 0
        open_t = self.conn.execute("SELECT COUNT(*) FROM tickets WHERE merchant_id=? AND status NOT IN ('CLOSED','RESOLVED','WITHDRAWN')", (mid,)).fetchone()[0]
        tickets = max(0, 20 - 5 * open_t)
        sentiment = round(10 * (max(-1.0, min(1.0, self.sentiments.get(mid, 0.0))) + 1) / 2)
        plan = self.conn.execute("SELECT status FROM plans WHERE merchant_id=?", (mid,)).fetchone()
        contract = {"ACTIVE": 10, "PENDING_ACTIVATION": 5}.get(plan["status"] if plan else None, 0 if plan is None else 2)
        comps = {"usage": usage, "device": device, "tickets": tickets, "sentiment": sentiment, "contract": contract}
        return sum(comps.values()), comps

    def _store_score(self, mid, as_of, score, comps, diagnosis, report: HealthReport) -> None:
        prev = self.conn.execute("SELECT score FROM merchant_health WHERE merchant_id=? AND as_of < ? ORDER BY as_of DESC LIMIT 1", (mid, as_of)).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO merchant_health VALUES (?,?,?,?,?)", (mid, as_of, score, json.dumps(comps, sort_keys=True), diagnosis))
        why = None
        if score < RETENTION_SCORE:
            why = f"health score {score} is below {RETENTION_SCORE}"
        elif prev is not None and prev[0] - score >= RETENTION_FALL:
            why = f"health score fell {prev[0] - score} points to {score}"
        if why:
            self.conn.execute("INSERT OR REPLACE INTO retention_queue VALUES (?,?,?,?)", (mid, as_of, score, why))
            report.retention.append(mid)
