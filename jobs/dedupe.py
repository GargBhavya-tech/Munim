"""Duplicate matching (#98; Bible section 9): no lead that is already a Paytm merchant reaches new-merchant outreach.

Each NEW lead is compared, on normalised name, phone, pincode and coordinates, with every Paytm merchant and then with the better leads
already kept. The outcome is one of:
  EXISTING_MERCHANT  a certain match with a merchant: ``matched_merchant_id`` is set; the merchant goes to S2-S5, never to new-merchant outreach
  DUPLICATE          a certain match with a better lead: kept once, the others point at it (``duplicate_of`` in provenance)
  NEEDS_REVIEW       a possible match: held back from outreach until a person looks (wrongly pitching an existing merchant is the costly error,
                     so doubt blocks)
  NEW (cleared)      no match: marked ``dedupe.result = CLEAR`` and only then eligible
``outreach_ready`` is the ONE query the outreach side may use; it demands the CLEAR mark, so a lead that was never matched cannot be pitched.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from core.engine.clock import SimClock
from core.memory.store import write_txn

LEGAL = frozenset({"pvt", "ltd", "private", "limited", "llp", "co", "company", "the", "and", "shop", "store"})
PHONE_DIGITS = 10
CERTAIN_NAME, CERTAIN_PHONE_NAME, CLOSE_NAME, POSSIBLE_NAME = 0.85, 0.5, 0.6, 0.7
GEO_SAME_PLACE_M, GEO_NEAR_M, GEO_AREA_M, GEO_REVIEW_M = 50.0, 150.0, 300.0, 1000.0
MATCH, REVIEW = "MATCH", "REVIEW"


def norm_name(s: str | None) -> str:
    t = unicodedata.normalize("NFKC", s or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(w for w in t.split() if w not in LEGAL)


def norm_phone(p: str | None) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-PHONE_DIGITS:] if len(d) >= PHONE_DIGITS else ""


def name_sim(a: str, b: str) -> float:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    return max(len(ta & tb) / len(ta | tb), SequenceMatcher(None, na, nb).ratio())


def metres(a: tuple, b: tuple) -> float | None:
    if None in (*a, *b):
        return None
    (la1, lo1), (la2, lo2) = a, b
    p1, p2, dphi, dl = math.radians(la1), math.radians(la2), math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def compare(a: dict, b: dict) -> tuple[str | None, list[str]]:
    """(MATCH | REVIEW | None, reasons). ``a``/``b`` have name, phone, pincode, lat, lng."""
    sim = name_sim(a["name"], b["name"])
    pa, pb = norm_phone(a.get("phone")), norm_phone(b.get("phone"))
    phone = bool(pa) and pa == pb
    same_pin = bool(a.get("pincode")) and a.get("pincode") == b.get("pincode")
    d = metres((a.get("lat"), a.get("lng")), (b.get("lat"), b.get("lng")))
    why = [f"name {sim:.2f}"] + (["phone"] if phone else []) + (["pincode"] if same_pin else []) + ([f"{d:.0f} m"] if d is not None else [])
    if d is not None:
        # Both places are located: distance arbitrates. The same name a few km away is another outlet, not the same shop.
        if (phone and sim >= CERTAIN_PHONE_NAME) or (sim >= CERTAIN_NAME and d <= GEO_NEAR_M) or (sim >= CLOSE_NAME and d <= GEO_SAME_PLACE_M):
            return MATCH, why
        if phone or (sim >= POSSIBLE_NAME and d <= GEO_REVIEW_M) or (sim >= CLOSE_NAME and d <= GEO_AREA_M):
            return REVIEW, why
        return None, why
    # One side has no coordinates: name, phone and pincode have to carry it.
    if (phone and sim >= CERTAIN_PHONE_NAME) or (sim >= CERTAIN_NAME and same_pin):
        return MATCH, why
    if phone or (sim >= POSSIBLE_NAME and same_pin):
        return REVIEW, why
    return None, why


def _lead(r: sqlite3.Row) -> dict:
    return {"id": r["lead_id"], "name": r["name"], "phone": r["phone_public"], "pincode": r["pincode"], "lat": r["lat"], "lng": r["lng"],
            "confidence": r["confidence"] or 0.0}


@dataclass
class DedupeResult:
    existing: list[str]
    duplicates: list[str]
    review: list[str]
    clear: list[str]


class DedupeJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger=None):
        self.conn, self.clock, self.ledger = conn, clock, ledger

    def _merchants(self) -> list[dict]:
        return [{"id": r["merchant_id"], "name": r["name"], "phone": r["phone"], "pincode": r["pincode"], "lat": r["lat"], "lng": r["lng"]}
                for r in self.conn.execute("SELECT merchant_id, name, phone, pincode, lat, lng FROM merchants")]

    def _set(self, lead_id: str, state: str, dedupe: dict, matched_merchant: str | None = None) -> None:
        prov = json.loads(self.conn.execute("SELECT provenance_json FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()[0] or "{}")
        prov["dedupe"] = {**dedupe, "checked_at": self.clock.now_iso()}
        self.conn.execute("UPDATE leads SET state = ?, matched_merchant_id = ?, provenance_json = ? WHERE lead_id = ?",
                          (state, matched_merchant, json.dumps(prov, sort_keys=True), lead_id))

    def run(self) -> DedupeResult:
        merchants = self._merchants()
        todo = [r for r in self.conn.execute("SELECT * FROM leads WHERE state = 'NEW' AND (provenance_json IS NULL OR provenance_json NOT LIKE '%\"dedupe\"%') "
                                             "ORDER BY confidence DESC, lead_id")]
        kept = [_lead(r) for r in self.conn.execute("SELECT * FROM leads WHERE state = 'NEW' AND provenance_json LIKE '%\"result\": \"CLEAR\"%'")]
        out = DedupeResult([], [], [], [])
        with write_txn(self.conn):
            for r in todo:
                lead = _lead(r)
                verdict = self._against(lead, merchants)
                if verdict:
                    kind, mid, why = verdict
                    if kind == MATCH:
                        self._set(lead["id"], "EXISTING_MERCHANT", {"result": "EXISTING_MERCHANT", "reasons": why}, mid)
                        out.existing.append(lead["id"])
                    else:
                        self._set(lead["id"], "NEEDS_REVIEW", {"result": "NEEDS_REVIEW", "possible_merchant": mid, "reasons": why})
                        out.review.append(lead["id"])
                    continue
                verdict = self._against(lead, kept)
                if verdict and verdict[0] == MATCH:
                    self._set(lead["id"], "DUPLICATE", {"result": "DUPLICATE", "duplicate_of": verdict[1], "reasons": verdict[2]})
                    out.duplicates.append(lead["id"])
                elif verdict:
                    self._set(lead["id"], "NEEDS_REVIEW", {"result": "NEEDS_REVIEW", "possible_lead": verdict[1], "reasons": verdict[2]})
                    out.review.append(lead["id"])
                else:
                    self._set(lead["id"], "NEW", {"result": "CLEAR"})
                    kept.append(lead)
                    out.clear.append(lead["id"])
            if self.ledger is not None:
                self.ledger.append(kind="STATE", actor="SYSTEM", payload={"job": "dedupe", "existing_merchant": len(out.existing), "duplicate": len(out.duplicates),
                                                                         "needs_review": len(out.review), "clear": len(out.clear)})
        return out

    @staticmethod
    def _against(lead: dict, others: list[dict]):
        """Best verdict against ``others``: a MATCH beats a REVIEW, and among matches the strongest evidence wins (a shared phone, then the
        shortest distance, then the closest name), so the lead points at the RIGHT merchant, not just a same-named one. (kind, id, reasons) or None."""
        best_match = best_review = None
        for o in others:
            kind, why = compare(lead, o)
            if kind is None:
                continue
            d = metres((lead.get("lat"), lead.get("lng")), (o.get("lat"), o.get("lng")))
            rank = (norm_phone(lead.get("phone")) != "" and norm_phone(lead.get("phone")) == norm_phone(o.get("phone")),
                    -(d if d is not None else 1e12), name_sim(lead["name"], o["name"]))
            if kind == MATCH and (best_match is None or rank > best_match[0]):
                best_match = (rank, (MATCH, o["id"], why))
            elif kind == REVIEW and (best_review is None or rank > best_review[0]):
                best_review = (rank, (REVIEW, o["id"], why))
        return (best_match or best_review or (None, None))[1]


def outreach_ready(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The only leads new-merchant outreach may take: matched against every merchant and every better lead, and clear."""
    return conn.execute("SELECT * FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND provenance_json LIKE '%\"result\": \"CLEAR\"%' "
                        "ORDER BY lead_id").fetchall()
