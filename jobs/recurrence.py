"""Repeated issues and root causes (Bible section 21) and fleet patterns (grouping rule, section 22).

A plain program over cases, tickets, devices and events. It writes its OWN rows (``chronic_flags``, ``root_cause_tasks``,
``monitoring_watch``, ``incident_candidates``, ``fleet_reports``) and never messages a merchant, raises a ticket or calls a mock.

* CHRONIC: the same issue 2+ times in 30 days (the Bible's rule; ``min_occurrences`` is configurable), the same device
  failing again, or the same root cause coming back. A repeat is never "just another ticket": it is a flag, ONE root-cause
  task for a SENIOR owner who gets the history, and proactive monitoring switched on.
* fleet: >= 10 devices in ONE PINCODE losing heartbeat within 10 minutes, or >= 10 merchants' settlements failing on ONE
  BANK within 30 minutes, is an incident candidate. Grouping by city is the named mistake and is not done here.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from core.engine.clock import SimClock, iso, parse_iso

WINDOW_DAYS = 30
MIN_OCCURRENCES = 2
FLEET_MIN = 10
HEARTBEAT_WINDOW = timedelta(minutes=10)
SETTLEMENT_WINDOW = timedelta(minutes=30)
WEAK_SIGNAL_PCT = 40
DEVICE_PLAYBOOKS = frozenset({"C2_SOUNDBOX_FAULT"})

CAUSES = {
    "FAULTY_DEVICE": "Replace the device; do not repair it again",
    "ENVIRONMENT": "Fix the shop: switch network, power adapter, or move the device",
    "MERCHANT_NEEDS_HELP": "Short video in the merchant's language and a proactive tip",
    "PAYTM_SIDE": "Group into an incident candidate and route to the owning team",
    "UNKNOWN": "Senior root-cause analysis",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chronic_flags (
  merchant_id TEXT NOT NULL, issue_type TEXT NOT NULL, flagged_at TEXT NOT NULL, occurrences INTEGER NOT NULL,
  reason TEXT NOT NULL, owner_tier TEXT NOT NULL, owner_id TEXT, monitoring INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (merchant_id, issue_type)
);
CREATE TABLE IF NOT EXISTS root_cause_tasks (
  task_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, issue_types_json TEXT NOT NULL, cause TEXT NOT NULL, action TEXT NOT NULL,
  owner_tier TEXT NOT NULL, owner_id TEXT, history_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitoring_watch (
  merchant_id TEXT PRIMARY KEY, since TEXT NOT NULL, reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS incident_candidates (
  candidate_id TEXT PRIMARY KEY, kind TEXT NOT NULL, area TEXT NOT NULL, member_count INTEGER NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT NOT NULL, members_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_reports (
  report_id TEXT PRIMARY KEY, kind TEXT NOT NULL, batch TEXT NOT NULL, member_count INTEGER NOT NULL, details_json TEXT NOT NULL,
  route_to TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def classify_root_cause(evidence: dict) -> tuple[str, str]:
    """The Bible's cause table. ``evidence``: fleet_merchants, weak_signal, same_device_repeat, how_to_repeat."""
    if evidence.get("fleet_merchants", 0) >= FLEET_MIN:
        cause = "PAYTM_SIDE"
    elif evidence.get("weak_signal"):
        cause = "ENVIRONMENT"
    elif evidence.get("same_device_repeat"):
        cause = "FAULTY_DEVICE"
    elif evidence.get("how_to_repeat"):
        cause = "MERCHANT_NEEDS_HELP"
    else:
        cause = "UNKNOWN"
    return cause, CAUSES[cause]


@dataclass
class RecurrenceReport:
    chronic: list[tuple[str, str]] = field(default_factory=list)      # (merchant_id, issue_type) newly or still flagged
    tasks: list[str] = field(default_factory=list)                    # task ids created by this run


class RecurrenceJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, *, senior_pool: tuple[str, ...] = ("SENIOR-1",),
                 min_occurrences: int = MIN_OCCURRENCES, window_days: int = WINDOW_DAYS):
        self.conn, self.clock = conn, clock
        self.senior_pool = tuple(senior_pool)
        self.min_occurrences, self.window = min_occurrences, timedelta(days=window_days)
        ensure_schema(conn)

    # ------------------------------------------------------------ per merchant
    def _triggers(self, mid: str) -> dict[str, list[dict]]:
        """issue_type -> the occurrences inside the window. Issue types: <playbook>, DEVICE:<id>, TICKET:<type>."""
        cutoff = iso(self.clock.now() - self.window)
        out: dict[str, list[dict]] = defaultdict(list)
        for c in self.conn.execute("SELECT case_id, playbook_id, state, opened_at, resume_note FROM cases WHERE merchant_id=? AND opened_at >= ? "
                                   "ORDER BY opened_at, case_id", (mid, cutoff)):
            out[c["playbook_id"]].append({"kind": "case", **dict(c)})
        for t in self.conn.execute("SELECT ticket_id, type, system_object, summary, created_at, status FROM tickets WHERE merchant_id=? "
                                   "AND created_at >= ? AND status != 'WITHDRAWN' ORDER BY created_at, ticket_id", (mid, cutoff)):
            out[f"TICKET:{t['type']}"].append({"kind": "ticket", **dict(t)})
            if t["system_object"] and t["system_object"].startswith("D-"):
                out[f"DEVICE:{t['system_object']}"].append({"kind": "ticket", **dict(t)})
        for r in self.conn.execute("SELECT replacement_id, device_id, reason, status, created_at FROM device_replacements WHERE merchant_id=? "
                                   "AND created_at >= ? AND status != 'CANCELLED' ORDER BY created_at", (mid, cutoff)):
            out[f"DEVICE:{r['device_id']}"].append({"kind": "replacement", **dict(r)})
        return out

    def _owner(self, mid: str) -> str:
        return self.senior_pool[zlib.crc32(mid.encode()) % len(self.senior_pool)]

    def _fleet_merchants(self, mid: str, playbooks: list[str]) -> int:
        """Other merchants, on the same bank, with the same issue in the window: the 'Paytm-side' sign."""
        if not playbooks:
            return 0
        bank = self.conn.execute("SELECT substr(ifsc,1,4) FROM bank WHERE merchant_id=?", (mid,)).fetchone()
        if not bank or not bank[0]:
            return 0
        cutoff = iso(self.clock.now() - self.window)
        ph = ",".join("?" * len(playbooks))
        return self.conn.execute(
            f"SELECT COUNT(DISTINCT c.merchant_id) FROM cases c JOIN bank b ON b.merchant_id=c.merchant_id WHERE c.playbook_id IN ({ph}) "
            "AND c.opened_at >= ? AND substr(b.ifsc,1,4)=? AND c.merchant_id != ?", (*playbooks, cutoff, bank[0], mid)).fetchone()[0]

    def check_merchant(self, mid: str, report: RecurrenceReport | None = None) -> RecurrenceReport:
        report = report if report is not None else RecurrenceReport()
        now = self.clock.now_iso()
        hits = {k: v for k, v in self._triggers(mid).items() if len(v) >= self.min_occurrences}
        if not hits:
            return report
        owner = self._owner(mid)
        for issue, occ in sorted(hits.items()):
            reason = f"{len(occ)} x {issue} in {self.window.days} days"
            cur = self.conn.execute("INSERT OR IGNORE INTO chronic_flags VALUES (?,?,?,?,?,?,?,1)",
                                    (mid, issue, now, len(occ), reason, "SENIOR", owner))
            if cur.rowcount == 0:
                self.conn.execute("UPDATE chronic_flags SET occurrences=?, reason=? WHERE merchant_id=? AND issue_type=?", (len(occ), reason, mid, issue))
            report.chronic.append((mid, issue))
        self.conn.execute("INSERT OR IGNORE INTO monitoring_watch VALUES (?,?,?,1)", (mid, now, "CHRONIC: " + ", ".join(sorted(hits))))
        if self.conn.execute("SELECT 1 FROM root_cause_tasks WHERE merchant_id=? AND status='OPEN'", (mid,)).fetchone() is None:
            report.tasks.append(self._create_task(mid, hits, owner, now))
        self.conn.commit()
        return report

    def _create_task(self, mid: str, hits: dict[str, list[dict]], owner: str, now: str) -> str:
        case_pbs = [k for k, v in hits.items() if any(o["kind"] == "case" for o in v)]
        dev = self.conn.execute("SELECT MIN(signal_pct) FROM devices WHERE merchant_id=?", (mid,)).fetchone()[0]
        same_device = any(k.startswith("DEVICE:") for k in hits) or any(pb in DEVICE_PLAYBOOKS for pb in case_pbs)
        evidence = {
            "fleet_merchants": self._fleet_merchants(mid, case_pbs),
            "weak_signal": dev is not None and dev < WEAK_SIGNAL_PCT,
            "same_device_repeat": same_device,
            "how_to_repeat": any("HOWTO" in k.upper() for k in hits),
        }
        cause, action = classify_root_cause(evidence)
        history = {
            "cases": [dict(r) for r in self.conn.execute("SELECT case_id, playbook_id, state, opened_at, closed_at, resume_note FROM cases WHERE merchant_id=? ORDER BY opened_at, case_id", (mid,))],
            "tickets": [dict(r) for r in self.conn.execute("SELECT ticket_id, type, status, system_object, summary, created_at FROM tickets WHERE merchant_id=? ORDER BY created_at, ticket_id", (mid,))],
            "replacements": [dict(r) for r in self.conn.execute("SELECT replacement_id, device_id, reason, status, created_at FROM device_replacements WHERE merchant_id=? ORDER BY created_at", (mid,))],
            "evidence": evidence,
        }
        task_id = f"RC-{mid}-{now[:10]}"
        self.conn.execute("INSERT OR IGNORE INTO root_cause_tasks(task_id, merchant_id, issue_types_json, cause, action, owner_tier, owner_id, history_json, status, created_at) "
                          "VALUES (?,?,?,?,?,?,?,?,'OPEN',?)", (task_id, mid, json.dumps(sorted(hits)), cause, action, "SENIOR", owner,
                                                                 json.dumps(history, sort_keys=True, default=str), now))
        if cause == "PAYTM_SIDE":
            self.conn.execute("INSERT OR IGNORE INTO incident_candidates VALUES (?,?,?,?,?,?,?,?)",
                              (f"IC-REPEAT-{mid}-{now[:10]}", "REPEAT_FLEET", "bank", evidence["fleet_merchants"] + 1, now, now, json.dumps([mid]), now))
        return task_id

    def run(self, merchant_ids: list[str] | None = None) -> RecurrenceReport:
        report = RecurrenceReport()
        ids = merchant_ids if merchant_ids is not None else [r[0] for r in self.conn.execute(
            "SELECT DISTINCT merchant_id FROM cases WHERE merchant_id IS NOT NULL UNION SELECT DISTINCT merchant_id FROM tickets ORDER BY 1")]
        for mid in ids:
            self.check_merchant(mid, report)
        return report

    def is_chronic(self, mid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM chronic_flags WHERE merchant_id=?", (mid,)).fetchone() is not None

    # ------------------------------------------------------------ fleet
    def _windows(self, items: list[tuple[str, str]], width: timedelta) -> list[tuple[str, str, list[str]]]:
        """items: (iso ts, key) sorted by ts. Returns (start, end, members) for each non-overlapping window with >= FLEET_MIN distinct keys."""
        found, i = [], 0
        while i < len(items):
            start = parse_iso(items[i][0])
            j, members = i, {}
            while j < len(items) and parse_iso(items[j][0]) - start <= width:
                members.setdefault(items[j][1], items[j][0])
                j += 1
            if len(members) >= FLEET_MIN:
                found.append((items[i][0], items[j - 1][0], sorted(members)))
                i = j
            else:
                i += 1
        return found

    def detect_incident_candidates(self, lookback: timedelta = timedelta(hours=24)) -> list[str]:
        """Heartbeats lost per PINCODE (10 min) and settlement failures per BANK (30 min). Never grouped by city."""
        now = self.clock.now()
        since = iso(now - lookback)
        out: list[str] = []
        pins = {r[0]: r[1] for r in self.conn.execute("SELECT merchant_id, pincode FROM merchants")}
        banks = {r[0]: r[1] for r in self.conn.execute("SELECT merchant_id, substr(ifsc,1,4) FROM bank")}

        def collect(etype: str, area_of: dict, width: timedelta, kind: str):
            per: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for e in self.conn.execute("SELECT merchant_id, payload_json, ts_sim FROM events WHERE type=? AND ts_sim >= ? ORDER BY ts_sim, seq", (etype, since)):
                area = area_of.get(e["merchant_id"])
                if not area:
                    continue
                key = (json.loads(e["payload_json"]).get("device_id") or e["merchant_id"]) if etype == "DEVICE_HEARTBEAT_LOST" else e["merchant_id"]
                per[area].append((e["ts_sim"], key))
            for area in sorted(per):
                for start, end, members in self._windows(per[area], width):
                    cid = f"IC-{kind}-{area}-{start}"
                    self.conn.execute("INSERT OR IGNORE INTO incident_candidates VALUES (?,?,?,?,?,?,?,?)",
                                      (cid, kind, area, len(members), start, end, json.dumps(members), self.clock.now_iso()))
                    out.append(cid)

        collect("DEVICE_HEARTBEAT_LOST", pins, HEARTBEAT_WINDOW, "HEARTBEAT_PINCODE")
        collect("SETTLEMENT_FAILED", banks, SETTLEMENT_WINDOW, "SETTLEMENT_BANK")
        self.conn.commit()
        return out

    def report_device_batches(self, min_devices: int = 5) -> list[str]:
        """The same device batch (model + install month) failing across many merchants goes to product and quality."""
        cutoff = iso(self.clock.now() - self.window)
        rows = self.conn.execute(
            "SELECT d.model, substr(d.installed_at,1,7) month, COUNT(DISTINCT d.device_id) n, GROUP_CONCAT(DISTINCT d.merchant_id) mids "
            "FROM device_replacements r JOIN devices d ON d.device_id = r.device_id WHERE r.created_at >= ? AND r.status != 'CANCELLED' "
            "GROUP BY 1,2 HAVING n >= ? ORDER BY 1,2", (cutoff, min_devices)).fetchall()
        out = []
        for r in rows:
            rid = f"FR-BATCH-{r['model']}-{r['month']}-{self.clock.now_iso()[:10]}"
            self.conn.execute("INSERT OR IGNORE INTO fleet_reports VALUES (?,?,?,?,?,?,?)",
                              (rid, "DEVICE_BATCH", f"{r['model']} / {r['month']}", r["n"], json.dumps({"merchants": sorted(r["mids"].split(","))}),
                               "product_quality", self.clock.now_iso()))
            out.append(rid)
        self.conn.commit()
        return out
