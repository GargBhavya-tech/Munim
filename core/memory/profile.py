"""Merchant profile facts with provenance (Bible section 33).

Rules enforced in code, not in a prompt:
* Only ``SYSTEM`` facts can justify an action (``facts_for_action``). A merchant claim is stored as ``MERCHANT_SAID``.
* History is never overwritten: a new value closes the old row's ``valid_to`` and inserts a new row.
* ``AGENT_INFERRED`` facts come from ``INFERRED_TAGS`` only, can never name a protected trait, and never feed price/targeting.
* Explicit merchant preferences win over inference/detection.
* PIN / OTP / password / full card numbers are never stored.
* Facts whose consent scope was withdrawn stop being used (restrictive facts such as DO_NOT_CONTACT stay in force).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from core.engine.clock import SimClock, parse_iso

SOURCES = ("SYSTEM", "MERCHANT_SAID", "HUMAN_AGENT", "AGENT_INFERRED")

# The only tags the agent may infer. None of these is a personal/protected trait.
INFERRED_TAGS = frozenset({
    "language", "digital_comfort_voice", "digital_comfort_text", "prefers_short_replies", "likely_busy_evenings",
    "asks_about_fees", "prefers_whatsapp", "prefers_call",
})
# Explicit preferences: a stated value always beats detection/inference.
PREFERENCE_KEYS = frozenset({"language", "call_after_9pm", "preferred_channel", "contact_window"})
# Never stored, for any source (DPDP / fairness, Bible section 37). Matched against key tokens by prefix.
PROTECTED_TRAIT_WORDS = (
    "religion", "religious", "caste", "community", "ethnic", "race", "racial", "tribe", "sect", "creed", "gender", "sexual",
    "political", "disabilit", "nationality", "origin", "ancestry", "skin",
)
# Never stored: credentials and card data.
SECRET_KEY_TOKENS = frozenset({"pin", "otp", "cvv", "cvc", "password", "passcode", "mpin", "upi_pin"})
_SECRET_VALUE = re.compile(r"(?i)\b(otp|pin|mpin|cvv|cvc|password|passcode)\b\D{0,20}\d{3,8}")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

DO_NOT_CONTACT = "do_not_contact"
DECLINED_PREFIX = "declined:"
RESTRICTIVE_PREFIXES = (DO_NOT_CONTACT, DECLINED_PREFIX)
MEMORY_SCOPE = "memory"  # consent.channel value meaning "merchant-level memory"

_RANK_STRICT = {"SYSTEM": 0, "HUMAN_AGENT": 1, "MERCHANT_SAID": 2, "AGENT_INFERRED": 3}
_RANK_PREF = {"MERCHANT_SAID": 0, "HUMAN_AGENT": 0, "SYSTEM": 1, "AGENT_INFERRED": 2}


class ProfileError(ValueError):
    pass


class ConsentWithdrawn(ProfileError):
    pass


@dataclass(frozen=True)
class Fact:
    fact_id: int
    merchant_id: str
    key: str
    value: str | None
    source: str
    evidence_ref: str | None
    confidence: float
    valid_from: str
    valid_to: str | None
    consent_scope: str | None

    @property
    def inferred(self) -> bool:
        return self.source == "AGENT_INFERRED"


def _tokens(key: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", key.lower()) if t]


def check_key(key: str) -> str:
    k = (key or "").strip().lower()
    if not k:
        raise ProfileError("empty fact key")
    toks = _tokens(k)
    for t in toks:
        for w in PROTECTED_TRAIT_WORDS:
            if t.startswith(w):
                raise ProfileError(f"fact key {key!r} names a protected trait ({w}); never stored")
    if any(t in SECRET_KEY_TOKENS for t in toks) or "card_number" in k or "cardnumber" in k or "aadhaar" in toks:
        raise ProfileError(f"fact key {key!r} would store a secret (PIN/OTP/password/card); never stored")
    return k


def check_value(value) -> str | None:
    if value is None:
        return None
    s = str(value)
    if _SECRET_VALUE.search(s):
        raise ProfileError("value contains a PIN/OTP/password; never stored")
    for m in _CARD.finditer(s):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19:
            raise ProfileError("value looks like a full card number; never stored")
    return s


def memory_allowed(conn: sqlite3.Connection, merchant_id: str, scope: str | None = None) -> bool:
    """False when the merchant withdrew consent for merchant-level memory (or for ``scope``)."""
    scopes = [MEMORY_SCOPE, "*"] + ([scope] if scope else [])
    q = ",".join("?" * len(scopes))
    return conn.execute(f"SELECT 1 FROM consent WHERE subject_id = ? AND status = 'REVOKED' AND channel IN ({q})",
                        (merchant_id, *scopes)).fetchone() is None


def _is_restrictive(key: str) -> bool:
    return key == DO_NOT_CONTACT or key.startswith(DECLINED_PREFIX)


def _row(r: sqlite3.Row) -> Fact:
    return Fact(r["fact_id"], r["merchant_id"], r["key"], r["value"], r["source"], r["evidence_ref"], r["confidence"],
                r["valid_from"], r["valid_to"], r["consent_scope"])


class ProfileStore:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock

    # -- writes ------------------------------------------------------------------------------------
    def set_fact(self, merchant_id: str, key: str, value, source: str, evidence_ref: str | None = None,
                 confidence: float | None = None, consent_scope: str | None = None) -> Fact:
        """Supersede (close ``valid_to`` + insert). Validity is per (merchant, key, source): a merchant claim never
        closes a SYSTEM row. Returns the inserted fact (or the existing identical one)."""
        if source not in SOURCES:
            raise ProfileError(f"unknown source {source!r}")
        k = check_key(key)
        v = check_value(value)
        if source == "AGENT_INFERRED" and k not in INFERRED_TAGS:
            raise ProfileError(f"{k!r} is not in the fixed inferred-tag list")
        if confidence is None:
            confidence = 1.0 if source in ("SYSTEM", "HUMAN_AGENT") else 0.7
        if source == "SYSTEM":
            confidence = 1.0
        if not 0.0 <= confidence <= 1.0:
            raise ProfileError("confidence must be within 0..1")
        if not _is_restrictive(k) and not memory_allowed(self.conn, merchant_id, consent_scope):
            raise ConsentWithdrawn(f"merchant {merchant_id} withdrew consent for memory; fact not stored")
        now = self.clock.now_iso()
        cur = self.conn.execute("SELECT * FROM profile_facts WHERE merchant_id=? AND key=? AND source=? AND valid_to IS NULL",
                                (merchant_id, k, source)).fetchone()
        if cur is not None and cur["value"] == v and cur["consent_scope"] == consent_scope:
            return _row(cur)  # nothing changed: no new history row
        own = not self.conn.in_transaction
        if own:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("UPDATE profile_facts SET valid_to=? WHERE merchant_id=? AND key=? AND source=? AND valid_to IS NULL",
                              (now, merchant_id, k, source))
            c = self.conn.execute(
                "INSERT INTO profile_facts(merchant_id,key,value,source,evidence_ref,confidence,valid_from,valid_to,consent_scope) "
                "VALUES (?,?,?,?,?,?,?,NULL,?)", (merchant_id, k, v, source, evidence_ref, confidence, now, consent_scope))
            fid = c.lastrowid
            if own:
                self.conn.execute("COMMIT")
        except BaseException:
            if own:
                self.conn.execute("ROLLBACK")
            raise
        return _row(self.conn.execute("SELECT * FROM profile_facts WHERE fact_id=?", (fid,)).fetchone())

    # -- reads -------------------------------------------------------------------------------------
    def _visible(self, merchant_id: str, facts: list[Fact]) -> list[Fact]:
        if not facts:
            return facts
        mem_ok = memory_allowed(self.conn, merchant_id)
        out = []
        for f in facts:
            if _is_restrictive(f.key):
                out.append(f)
            elif mem_ok and (f.consent_scope is None or memory_allowed(self.conn, merchant_id, f.consent_scope)):
                out.append(f)
        return out

    def history(self, merchant_id: str, key: str | None = None) -> list[Fact]:
        """Every row ever written (open and closed), oldest first. Not consent-filtered: an audit view."""
        if key is None:
            rs = self.conn.execute("SELECT * FROM profile_facts WHERE merchant_id=? ORDER BY fact_id", (merchant_id,)).fetchall()
        else:
            rs = self.conn.execute("SELECT * FROM profile_facts WHERE merchant_id=? AND key=? ORDER BY fact_id",
                                   (merchant_id, key.strip().lower())).fetchall()
        return [_row(r) for r in rs]

    def _open(self, merchant_id: str) -> list[Fact]:
        rs = self.conn.execute("SELECT * FROM profile_facts WHERE merchant_id=? AND valid_to IS NULL ORDER BY fact_id", (merchant_id,))
        return [_row(r) for r in rs]

    def current(self, merchant_id: str) -> dict[str, Fact]:
        """One winning fact per key. Preferences: the newest explicit statement beats inference.
        Otherwise SYSTEM > HUMAN_AGENT > MERCHANT_SAID > AGENT_INFERRED, newest first within a source."""
        best: dict[str, Fact] = {}
        for f in self._visible(merchant_id, self._open(merchant_id)):
            rank = _RANK_PREF if f.key in PREFERENCE_KEYS else _RANK_STRICT
            cur = best.get(f.key)
            if cur is None or (rank[f.source], -f.fact_id) < (rank[cur.source], -cur.fact_id):
                best[f.key] = f
        return best

    def get(self, merchant_id: str, key: str) -> Fact | None:
        return self.current(merchant_id).get(key.strip().lower())

    def facts_for_action(self, merchant_id: str) -> dict[str, Fact]:
        """ONLY SYSTEM facts. Merchant claims, human notes and inferences never unlock an action."""
        return {f.key: f for f in self._visible(merchant_id, self._open(merchant_id)) if f.source == "SYSTEM"}

    # Inferred facts (and claims) never change prices or targeting: same restricted view on purpose.
    facts_for_pricing_or_targeting = facts_for_action

    # -- cross-case facts --------------------------------------------------------------------------
    def decline(self, merchant_id: str, offer: str, until: datetime | str, source: str = "MERCHANT_SAID",
                evidence_ref: str | None = None) -> Fact:
        until_s = until if isinstance(until, str) else until.isoformat()
        return self.set_fact(merchant_id, f"{DECLINED_PREFIX}{offer.lower()}", until_s, source, evidence_ref)

    def is_declined(self, merchant_id: str, offer: str) -> bool:
        """True while the DECLINED(offer, until) window is open, in every case."""
        for f in self._open(merchant_id):
            if f.key == f"{DECLINED_PREFIX}{offer.lower()}" and f.value:
                if parse_iso(f.value) > self.clock.now():
                    return True
        return False

    def declined_until(self, merchant_id: str) -> dict[str, str]:
        now = self.clock.now()
        return {f.key[len(DECLINED_PREFIX):].upper(): f.value for f in self._open(merchant_id)
                if f.key.startswith(DECLINED_PREFIX) and f.value and parse_iso(f.value) > now}

    def set_do_not_contact(self, merchant_id: str, source: str = "MERCHANT_SAID", evidence_ref: str | None = None) -> Fact:
        return self.set_fact(merchant_id, DO_NOT_CONTACT, "true", source, evidence_ref)

    def is_do_not_contact(self, merchant_id: str) -> bool:
        """Honoured from ANY source (it only ever restricts), and in every case."""
        return any(f.key == DO_NOT_CONTACT and (f.value or "").lower() == "true" for f in self._open(merchant_id))
