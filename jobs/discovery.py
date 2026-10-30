"""Discovery job (#97; Bible section 9): candidate leads from three layers, each with provenance, business data only.

  Layer 1  Paytm's own data (no scraping, no registry): registered-but-no-device merchants ranked by 30-day QR volume, loan-ready device
           merchants, dormant merchants, abandoned signups, inbound loan intent. These are EXISTING merchants, so they go to ``upsell_targets``
           (S2-S5), never to new-merchant outreach.
  Layer 2  Official registries (GST, MCA, Udyam, FSSAI, ONDC) and Layer 3 web signals (Places API, permitted pages): fetched ONLY through
           ``SourceGate.fetch``, so an unregistered, manual_only or rate-limited source is refused and ledgered. Each record is cut down to
           business fields by ``business_only`` and stored in ``leads`` with the source, fetch time and confidence of every field.
What is stored: name, category, address, pincode, coordinates, a PUBLIC BUSINESS phone, GST slab. What is never stored: an owner's or contact's
name, a personal phone, an email, an ID number, or anything else a page happened to carry; those are dropped and only counted.
Stored leads are state NEW and un-deduplicated; ``jobs.dedupe`` decides whether they may be pitched.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping

from core.engine.clock import SimClock, iso
from core.memory.store import next_id, write_txn
from jobs.dedupe import norm_name
from jobs.sources import SourceGate, SourceRefused

STORED_FIELDS = ("name", "category", "address", "pincode", "lat", "lng", "phone_public", "gst_slab")
FIELD_ALIASES = {"phone": "phone_public"}
LAYER_2_3 = ("gst_api", "mca", "udyam", "fssai", "ondc", "places_api", "company_websites", "mall_directories")
DORMANT_DAYS = 14
LOAN_READY_MIN_PAYMENTS = 20
UPSELL_SCHEMA = """
CREATE TABLE IF NOT EXISTS upsell_targets (
  merchant_id TEXT NOT NULL, reason TEXT NOT NULL, rank_value INTEGER NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (merchant_id, reason)
);
"""


def business_only(raw: Mapping[str, Any]) -> tuple[dict, list[str]]:
    """(the business fields to store, the names of the fields that were dropped). A record with no business name yields ({}, [...])."""
    out: dict[str, Any] = {}
    dropped: list[str] = []
    kind = str(raw.get("phone_kind") or "").lower()
    for k, v in raw.items():
        key = FIELD_ALIASES.get(k, k)
        if key in STORED_FIELDS and v not in (None, ""):
            if key == "phone_public" and kind != "business":
                dropped.append(k)                         # a phone not labelled as a public business line is treated as personal
                continue
            out[key] = v
        else:
            dropped.append(k)
    if not str(out.get("name") or "").strip():
        return {}, dropped
    return out, dropped


@dataclass
class DiscoveryResult:
    stored: list[str] = field(default_factory=list)          # new lead ids
    skipped_existing: int = 0                                # already stored from the same source
    refused: dict[str, str] = field(default_factory=dict)    # source id -> why the registry said no
    dropped_fields: dict[str, int] = field(default_factory=dict)
    upsell_targets: int = 0


class DiscoveryJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, gate: SourceGate | None = None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        self.gate = gate or SourceGate(conn, clock, ledger)
        for stmt in UPSELL_SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)

    # ------------------------------------------------------------------------------------------------ Layer 1
    def paytm_data(self) -> int:
        """Existing merchants worth an S2-S5 conversation. Ranked by their own 30-day QR volume (system rows, never a model)."""
        now = self.clock.now()
        since30, since_d = iso(now - timedelta(days=30)), iso(now - timedelta(days=DORMANT_DAYS))
        rows: list[tuple[str, str, int, dict]] = []
        vol = {r["merchant_id"]: (r["paise"], r["n"], r["last"]) for r in self.conn.execute(
            "SELECT merchant_id, COALESCE(SUM(amount_paise), 0) paise, COUNT(*) n, MAX(ts) last FROM payments "
            "WHERE status = 'SUCCESS' AND ts >= ? GROUP BY merchant_id", (since30,))}
        for m in self.conn.execute("SELECT merchant_id FROM merchants ORDER BY merchant_id"):
            mid = m["merchant_id"]
            paise, n, last = vol.get(mid, (0, 0, None))
            has_device = self.conn.execute("SELECT 1 FROM devices WHERE merchant_id = ?", (mid,)).fetchone() is not None
            if not has_device:
                rows.append((mid, "REGISTERED_NO_DEVICE", paise, {"payments_30d": n}))
            elif n >= LOAN_READY_MIN_PAYMENTS and not self.conn.execute("SELECT 1 FROM lender_apps WHERE merchant_id = ?", (mid,)).fetchone():
                rows.append((mid, "LOAN_READY", paise, {"payments_30d": n}))
            if has_device and (last is None or last < since_d):
                rows.append((mid, "DORMANT", 0, {"last_payment": last}))
            kyc = self.conn.execute("SELECT status FROM kyc WHERE merchant_id = ?", (mid,)).fetchone()
            if kyc and kyc["status"] != "APPROVED":
                rows.append((mid, "ABANDONED_SIGNUP", 0, {"kyc": kyc["status"]}))
        for e in self.conn.execute("SELECT DISTINCT merchant_id FROM events WHERE type IN ('MERCHANT_MESSAGE', 'LEAD_REPLY') AND payload_json LIKE '%LOAN_INTEREST%'"):
            if e["merchant_id"]:
                rows.append((e["merchant_id"], "INBOUND_INTENT", 0, {"intent": "LOAN_INTEREST"}))
        with write_txn(self.conn):
            for mid, reason, rank, detail in rows:
                self.conn.execute("INSERT OR REPLACE INTO upsell_targets VALUES (?,?,?,?,?)", (mid, reason, rank, json.dumps({**detail, "source": "paytm_data"}), self.clock.now_iso()))
        return len(rows)

    # ------------------------------------------------------------------------------------------------ Layers 2 and 3
    def discover(self, fetchers: Mapping[str, Callable[[list[str]], list[Mapping[str, Any]]]], pincodes: list[str], *, confidence: float = 0.8) -> DiscoveryResult:
        """``fetchers``: source id -> callable(pincodes) -> raw records. Every call goes through the source registry."""
        res = DiscoveryResult()
        for source_id, fetcher in fetchers.items():
            try:
                raws = self.gate.fetch(source_id, fetcher, pincodes)
            except SourceRefused as e:
                res.refused[source_id] = e.reason
                continue
            fetched_at = self.clock.now_iso()
            for raw in raws:
                fields, dropped = business_only(raw)
                for d in dropped:
                    res.dropped_fields[d] = res.dropped_fields.get(d, 0) + 1
                if not fields:
                    continue
                if self._known(source_id, fields):
                    res.skipped_existing += 1
                    continue
                res.stored.append(self._store(source_id, fields, fetched_at, float(raw.get("confidence", confidence))))
        res.upsell_targets = self.paytm_data()
        self.ledger.append(kind="STATE", actor="SYSTEM", payload={"job": "discovery", "stored": len(res.stored), "refused_sources": sorted(res.refused),
                                                                 "dropped_fields": res.dropped_fields, "upsell_targets": res.upsell_targets})
        return res

    def _known(self, source_id: str, f: dict) -> bool:
        key, pin = norm_name(f["name"]), f.get("pincode")
        return any(norm_name(r["name"]) == key and r["pincode"] == pin for r in self.conn.execute("SELECT name, pincode FROM leads WHERE source_id = ?", (source_id,)))

    def _store(self, source_id: str, f: dict, fetched_at: str, confidence: float) -> str:
        prov = {k: {"source": source_id, "fetched_at": fetched_at, "confidence": confidence} for k in f}
        with write_txn(self.conn):
            lid = next_id(self.conn, "L", 4)
            self.conn.execute("INSERT INTO leads(lead_id, name, category, address, pincode, lat, lng, phone_public, gst_slab, source_id, fetched_at, confidence, state, provenance_json) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'NEW', ?)",
                              (lid, f["name"], f.get("category"), f.get("address"), f.get("pincode"), f.get("lat"), f.get("lng"), f.get("phone_public"), f.get("gst_slab"),
                               source_id, fetched_at, confidence, json.dumps(prov, sort_keys=True)))
        return lid
