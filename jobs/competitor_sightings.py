"""A rival's QR code seen in a street photo: from a rep's photo to the fact the health job acts on.

The retention diagnosis (``jobs/health.py``) calls a persistent dip a COMPETITOR_SWITCH only when a rival's QR is on record for the shop (the profile fact
``competitor_qr_seen``). This module is how that fact comes to exist without a person typing it in: a field rep takes a photo of the shop front, a reader extracts FIELDS
from it (is a QR visible, which network, how sure), and code decides what to do with them.

  * the reader only ever returns fields. A reading that carries a verdict ("is_competitor", "switched", ...) is refused, the same rule as Scam Shield's screenshot reader:
    the photo never decides anything, code does;
  * CONFIRMED needs a visible QR, a network on the known-rivals list, and confidence at or above ``MIN_CONFIDENCE``. Only then is the fact written (as a human-agent fact,
    with the photo reference as evidence), so the dip diagnosis can use it;
  * a reading that is unsure or names a network we do not know is NEEDS_REVIEW: a person looks at the photo and accepts or rejects it. Nothing is written until they do;
  * no QR visible is REJECTED. Every outcome is ledgered with ids only, never the photo.

The rival networks are SYNTHETIC placeholders. The reader itself (a vision model) is injected by the caller; none is wired live here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

from core.memory.profile import ProfileStore
from core.memory.store import next_id, write_txn

SCHEMA = ("CREATE TABLE IF NOT EXISTS competitor_sightings (sighting_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, rep_id TEXT NOT NULL, photo_ref TEXT NOT NULL, "
          "network TEXT, confidence REAL NOT NULL, status TEXT NOT NULL, reason TEXT, seen_at TEXT NOT NULL, reviewed_by TEXT)")
FACT_KEY = "competitor_qr_seen"
RIVALS = {"rival_a": "Rival network A", "rival_b": "Rival network B", "rival_c": "Rival network C"}      # SYNTHETIC
MIN_CONFIDENCE = 0.7                                                                                  # [ASSUMPTION]
REVIEW_ROLES = frozenset({"sales_lead", "ops_admin"})
CONFIRMED, NEEDS_REVIEW, REJECTED = "CONFIRMED", "NEEDS_REVIEW", "REJECTED"
READING_FIELDS = frozenset({"qr_visible", "network", "confidence"})


class BadReading(ValueError):
    pass


class NotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class Sighting:
    sighting_id: str
    merchant_id: str
    rep_id: str
    photo_ref: str
    network: str | None
    confidence: float
    status: str
    reason: str | None
    seen_at: str
    reviewed_by: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def validate_reading(reading: dict) -> dict:
    """Fields only. Anything beyond qr_visible, network and confidence (a verdict above all) is refused, not ignored."""
    if not isinstance(reading, dict) or set(reading) - READING_FIELDS or "qr_visible" not in reading or "confidence" not in reading:
        raise BadReading(f"a photo reading has exactly these fields: {sorted(READING_FIELDS)}; it never carries a verdict")
    c = reading["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0.0 <= c <= 1.0:
        raise BadReading("confidence is a number from 0 to 1")
    if not isinstance(reading["qr_visible"], bool):
        raise BadReading("qr_visible is true or false")
    n = reading.get("network")
    if n is not None and not isinstance(n, str):
        raise BadReading("network is a name or null")
    return {"qr_visible": reading["qr_visible"], "network": (n or "").strip().lower() or None, "confidence": float(c)}


class SightingBook:
    def __init__(self, conn: sqlite3.Connection, clock, ledger=None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        conn.execute(SCHEMA)

    def _row(self, r) -> Sighting:
        return Sighting(r["sighting_id"], r["merchant_id"], r["rep_id"], r["photo_ref"], r["network"], r["confidence"], r["status"], r["reason"], r["seen_at"], r["reviewed_by"])

    def _fact(self, s_merchant: str, network: str, photo_ref: str) -> None:
        ProfileStore(self.conn, self.clock).set_fact(s_merchant, FACT_KEY, network, "HUMAN_AGENT", evidence_ref=photo_ref)

    def _led(self, what: str, sighting_id: str, status: str) -> None:
        if self.ledger is not None:
            self.ledger.append(kind="STATE", actor="SYSTEM", payload={"competitor_sighting": what, "sighting_id": sighting_id, "status": status})

    def record(self, rep_id: str, merchant_id: str, photo_ref: str, reading: dict) -> Sighting:
        r = validate_reading(reading)
        if not photo_ref or not photo_ref.strip():
            raise BadReading("a sighting needs the photo it came from")
        if self.conn.execute("SELECT 1 FROM reps WHERE rep_id = ?", (rep_id,)).fetchone() is None:
            raise KeyError(rep_id)
        if self.conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone() is None:
            raise KeyError(merchant_id)
        if not r["qr_visible"] or r["network"] is None:
            status, reason = REJECTED, "no rival QR code is visible in the photo"
        elif r["network"] not in RIVALS:
            status, reason = NEEDS_REVIEW, f"the network {r['network']!r} is not on the list of known rivals"
        elif r["confidence"] < MIN_CONFIDENCE:
            status, reason = NEEDS_REVIEW, f"the reader is only {round(r['confidence'] * 100)}% sure"
        else:
            status, reason = CONFIRMED, None
        with write_txn(self.conn):
            sid = next_id(self.conn, "SG")
            self.conn.execute("INSERT INTO competitor_sightings VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                              (sid, merchant_id, rep_id, photo_ref.strip(), r["network"], r["confidence"], status, reason, self.clock.now_iso()))
            if status == CONFIRMED:
                self._fact(merchant_id, r["network"], photo_ref.strip())
            self._led("recorded", sid, status)
        return self.get(sid)

    def review(self, sighting_id: str, role: str, who: str, accept: bool) -> Sighting:
        """A person looks at the photo. Only a sales lead or ops admin may; only a NEEDS_REVIEW sighting can be reviewed; accepting it needs a network we know."""
        if role not in REVIEW_ROLES:
            raise NotAllowed(f"role {role!r} may not review a sighting")
        s = self.get(sighting_id)
        if s.status != NEEDS_REVIEW:
            raise ValueError(f"{sighting_id} is {s.status}: only a sighting waiting for review can be reviewed")
        if accept and s.network not in RIVALS:
            raise ValueError("a sighting can only be accepted for a network on the known-rivals list")
        status = CONFIRMED if accept else REJECTED
        with write_txn(self.conn):
            self.conn.execute("UPDATE competitor_sightings SET status = ?, reviewed_by = ? WHERE sighting_id = ?", (status, f"{role}:{who}", sighting_id))
            if accept:
                self._fact(s.merchant_id, s.network, s.photo_ref)
            self._led("reviewed", sighting_id, status)
        return self.get(sighting_id)

    def get(self, sighting_id: str) -> Sighting:
        r = self.conn.execute("SELECT * FROM competitor_sightings WHERE sighting_id = ?", (sighting_id,)).fetchone()
        if r is None:
            raise KeyError(sighting_id)
        return self._row(r)

    def all(self, merchant_id: str | None = None) -> list[Sighting]:
        try:
            q, a = ("SELECT * FROM competitor_sightings WHERE merchant_id = ? ORDER BY sighting_id", (merchant_id,)) if merchant_id else ("SELECT * FROM competitor_sightings ORDER BY sighting_id", ())
            return [self._row(r) for r in self.conn.execute(q, a)]
        except sqlite3.OperationalError:
            return []
