"""Source registry gate (#96; Bible section 9, assurance A28 "a forbidden source is never scraped").

Every fetch of lead data goes through ``SourceGate.fetch``. The registry (``sources`` table) says how a source may be used:

  official_api / licensed_api   allowed, rate limited
  permitted_scrape              allowed only while robots_ok is 1, rate limited, and the registry entry has been reviewed within its refresh_days
  manual_only                   NEVER fetched by code (Zomato, Swiggy, IndiaMART, JustDial terms forbid scraping); a person may research them by hand

A source that is not in the registry is refused (default deny): code cannot reach a URL by inventing a source id. Each decision, allowed or
refused, is one ledger entry (``kind=POLICY``, ``rule_ids=["SOURCE-REGISTRY"]``), so "why did we never fetch X" is answerable. The rate limit
is counted from the ledger on the simulated clock, never the wall clock. The fetcher itself is injected (a mock in the demo).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from core.engine.clock import SimClock, parse_iso

ACCESS = ("official_api", "licensed_api", "permitted_scrape", "manual_only")
RULE_ID = "SOURCE-REGISTRY"


class SourceRefused(Exception):
    def __init__(self, source_id: str, reason: str):
        super().__init__(f"{source_id}: {reason}")
        self.source_id, self.reason = source_id, reason


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    access: str
    terms_url: str | None
    robots_ok: bool
    rate_limit_per_min: int
    last_reviewed: str | None
    refresh_days: int | None


class SourceGate:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger):
        self.conn, self.clock, self.ledger = conn, clock, ledger

    def get(self, source_id: str) -> Source | None:
        r = self.conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if r is None:
            return None
        return Source(r["source_id"], r["name"], r["access"], r["terms_url"], bool(r["robots_ok"]), int(r["rate_limit_per_min"] or 0),
                      r["last_reviewed"], r["refresh_days"])

    def check(self, source_id: str) -> tuple[bool, str]:
        """(allowed, reason). Pure decision: nothing is fetched or recorded."""
        s = self.get(source_id)
        if s is None:
            return False, "not in the source registry (default deny)"
        if s.access not in ACCESS:
            return False, f"unknown access class {s.access!r}"
        if s.access == "manual_only":
            return False, "manual_only: the source's terms forbid automated access; research it by hand"
        if s.rate_limit_per_min <= 0:
            return False, "no rate limit is set for this source"
        if s.access == "permitted_scrape":
            if not s.robots_ok:
                return False, "robots.txt does not allow this"
            if s.last_reviewed is None or (s.refresh_days and self.clock.now() - parse_iso(s.last_reviewed) > timedelta(days=int(s.refresh_days))):
                return False, "the registry entry is overdue for review"
        if self._recent_fetches(source_id) >= s.rate_limit_per_min:
            return False, f"rate limit of {s.rate_limit_per_min} per minute reached"
        return True, s.access

    def _recent_fetches(self, source_id: str) -> int:
        since = (self.clock.now() - timedelta(minutes=1)).isoformat()
        rows = self.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'POLICY' AND ts_sim > ? AND payload_json LIKE ?",
                                 (since, f'%"source_id":"{source_id}"%')).fetchall()
        return sum(1 for r in rows if '"allow":true' in r[0])

    def fetch(self, source_id: str, fetcher: Callable[..., Any], *args, **kwargs) -> Any:
        """The ONLY way lead data is fetched. Raises SourceRefused (and ledgers it) when the registry says no."""
        ok, why = self.check(source_id)
        self.ledger.append(kind="POLICY", actor="SYSTEM", payload={"action": "fetch_source", "source_id": source_id, "allow": ok,
                                                                    "rule_ids": [] if ok else [RULE_ID], "reason": why})
        if not ok:
            raise SourceRefused(source_id, why)
        return fetcher(*args, **kwargs)
