"""Personalisation: who the merchant is to talk to (ContactProfile), how to talk (StyleHint), and what is being offered (OfferFacts).
Bible section 12, ticket #76.

Three rules are enforced by the shape of the code, not by a prompt:

* The profile is BUILT FROM DATA: the merchant's own settings (language, stated contact window), consent rows, the interaction
  log, the payment curve and the profile preferences. It never reads the merchant's name, and never reads a protected trait: the
  profile-fact keys it looks at are a fixed whitelist. Region is not used to guess a language.
* An explicit preference (a merchant or human agent statement in profile_facts) always beats everything inferred.
* STYLE varies, FACTS do not. ``OfferFacts`` is a separate frozen object built only from system-read tool results; ``StyleHint``
  carries no number at all. A renderer (built elsewhere) receives both and cannot change the first.

No wall clock: ``now`` comes from the injected SimClock. All money is integer paise.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from core.engine.clock import SimClock, iso, parse_iso
from core.memory.profile import ProfileStore

LANGUAGES = ("kn-IN", "hi-IN", "en-IN", "hinglish")
_LANG_ALIASES = {"kn": "kn-IN", "hi": "hi-IN", "en": "en-IN", "kn-in": "kn-IN", "hi-in": "hi-IN", "en-in": "en-IN", "hinglish": "hinglish", "hi-latn": "hinglish"}
CHANNELS = ("whatsapp", "app", "soundbox", "sms", "email", "voice")
DEFAULT_CHANNEL_ORDER = ("whatsapp", "app", "soundbox", "sms", "email", "voice")   # among the channels the merchant has consented to
TEXT_CHANNELS = ("app", "whatsapp", "email", "sms")
VOICE_CHANNELS = ("voice", "soundbox")
FORMALITY = ("warm", "friendly", "professional", "formal")
DECISION_MAKERS = ("owner", "owner_and_accountant", "committee")
DIGITAL_COMFORT = ("low", "medium", "high")
TRUST = ("new", "established", "strained")

# The only profile-fact keys this module reads. Anything else (above all anything about a person's background) is ignored.
FACT_KEYS = frozenset({"language", "preferred_channel", "contact_window", "call_after_9pm", "prefers_whatsapp", "prefers_call",
                       "digital_comfort_voice", "digital_comfort_text", "decision_maker", "formality"})
EXPLICIT_SOURCES = frozenset({"MERCHANT_SAID", "HUMAN_AGENT"})
MIN_INFERENCE_CONFIDENCE = 0.6

# Formality by segment: the Bible's segment spectrum (section 12). A stated formality beats this.
SEGMENT_FORMALITY = {"kirana": "warm", "chemist": "warm", "restaurant": "friendly", "d2c": "professional", "enterprise": "formal"}
NEW_MERCHANT_DAYS = 30
PAYMENT_WINDOW_DAYS = 30
CONTACT_HOURS = range(9, 21)         # the quietest hour is searched inside these hours only (the contact budget's quiet hours are 21:00-09:00)
MIN_PAYMENTS_FOR_CURVE = 10
DEFAULT_BEST_TIME = "11:00"
VOICE_NOTE_SECONDS = 20              # Bible section 12: a 20-second voice note, one idea


# ------------------------------------------------------------------------------------------------ contact profile
@dataclass(frozen=True)
class ContactProfile:
    merchant_id: str
    language: str
    preferred_channel: str
    channel_consented: bool          # False when the merchant asked for a channel they have not consented to (the policy still blocks the send)
    digital_comfort: str
    decision_maker: str
    formality: str
    best_time: str                   # "HH:MM", the start of the best contact window
    trust_level: str
    provenance: Mapping[str, str] = field(default_factory=dict, compare=False)   # field -> explicit | data | default


def _norm_language(v: str | None) -> str | None:
    return _LANG_ALIASES.get((v or "").strip().lower())


def _hhmm(v: str | None) -> str | None:
    """The start of 'HH:MM' or 'HH:MM-HH:MM'; None if it is not a time."""
    if not v:
        return None
    head = v.strip().split("-")[0].strip()
    try:
        h, m = head.split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None


def quietest_hour(conn: sqlite3.Connection, merchant_id: str, now: datetime) -> str | None:
    """The contact hour with the fewest successful payments over the last 30 days ('HH:00'); ties go to the earlier hour.
    None when there is too little payment history to say."""
    cutoff = iso(now - timedelta(days=PAYMENT_WINDOW_DAYS))
    rows = conn.execute("SELECT ts FROM payments WHERE merchant_id = ? AND status = 'SUCCESS' AND ts >= ?", (merchant_id, cutoff)).fetchall()
    if len(rows) < MIN_PAYMENTS_FOR_CURVE:
        return None
    counts = {h: 0 for h in CONTACT_HOURS}
    for (ts,) in rows:
        h = parse_iso(ts).hour
        if h in counts:
            counts[h] += 1
    return f"{min(counts, key=lambda h: (counts[h], h)):02d}:00"


def build_contact_profile(conn: sqlite3.Connection, clock: SimClock, merchant_id: str) -> ContactProfile:
    now = clock.now()
    m = conn.execute("SELECT language, segment, registered_at, contact_window_start FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    if m is None:
        raise KeyError(merchant_id)
    facts = {k: f for k, f in ProfileStore(conn, clock).current(merchant_id).items() if k in FACT_KEYS}
    prov: dict[str, str] = {}

    def explicit(key: str):
        f = facts.get(key)
        return f if f is not None and f.source in EXPLICIT_SOURCES and f.value else None

    def inferred(key: str):
        f = facts.get(key)
        return f if f is not None and f.source not in EXPLICIT_SOURCES and f.value and f.confidence >= MIN_INFERENCE_CONFIDENCE else None

    consent = {r["channel"] for r in conn.execute("SELECT channel FROM consent WHERE subject_id = ? AND status = 'GRANTED'", (merchant_id,))}

    # language: the merchant's own statement, then their own app/Soundbox setting. Never the name, never the region.
    f = explicit("language")
    lang = _norm_language(f.value) if f else None
    prov["language"] = "explicit" if lang else "data"
    lang = lang or _norm_language(m["language"]) or "en-IN"

    # preferred channel: stated, then inferred tag, then what they actually reply on, then the first consented channel
    inbound = {r["channel"]: r["n"] for r in conn.execute(
        "SELECT channel, COUNT(*) n FROM contact_touches WHERE subject_id = ? AND direction = 'IN' GROUP BY channel", (merchant_id,))}
    channel, how = None, "data"
    f = explicit("preferred_channel")
    if f and f.value.strip().lower() in CHANNELS:
        channel, how = f.value.strip().lower(), "explicit"
    elif inferred("prefers_whatsapp") and (inferred("prefers_whatsapp").value or "").lower() == "true":
        channel = "whatsapp"
    elif inferred("prefers_call") and (inferred("prefers_call").value or "").lower() == "true":
        channel = "voice"
    elif any(c in inbound for c in CHANNELS):
        channel = max((c for c in CHANNELS if c in inbound), key=lambda c: (inbound[c], -CHANNELS.index(c)))
    else:
        channel = next((c for c in DEFAULT_CHANNEL_ORDER if c in consent), None)
        if channel is None:
            channel, how = "app", "default"
    if consent and channel not in consent and how != "explicit":                    # a channel they use but have not consented to would be refused (and the case escalated): reply on one they have
        channel, how = next((c for c in DEFAULT_CHANNEL_ORDER if c in consent), channel), "consented"
    prov["preferred_channel"] = how

    # digital comfort: stated or inferred voice/text preference, else the reply mix
    comfort, how = "medium", "default"
    if explicit("digital_comfort_voice") or inferred("digital_comfort_voice"):
        comfort, how = "low", "explicit" if explicit("digital_comfort_voice") else "data"
    elif explicit("digital_comfort_text") or inferred("digital_comfort_text"):
        comfort, how = "high", "explicit" if explicit("digital_comfort_text") else "data"
    elif any(c in inbound for c in VOICE_CHANNELS):
        comfort, how = "low", "data"
    elif sum(inbound.get(c, 0) for c in TEXT_CHANNELS) >= 3:
        comfort, how = "high", "data"
    prov["digital_comfort"] = how

    f = explicit("decision_maker")
    dm = f.value.strip().lower() if f and f.value.strip().lower() in DECISION_MAKERS else None
    prov["decision_maker"] = "explicit" if dm else "default"
    dm = dm or "owner"

    f = explicit("formality")
    formality = f.value.strip().lower() if f and f.value.strip().lower() in FORMALITY else None
    prov["formality"] = "explicit" if formality else "data"
    formality = formality or SEGMENT_FORMALITY.get(m["segment"], "friendly")

    # best time: a stated window, a window the merchant set, the quietest payment hour, then a fixed default
    best, how = None, "data"
    if explicit("contact_window"):
        best, how = _hhmm(explicit("contact_window").value), "explicit"
    if best is None and (f := explicit("call_after_9pm")) and f.value.lower() == "true":
        best, how = "21:00", "explicit"
    if best is None and m["contact_window_start"]:
        best, how = _hhmm(m["contact_window_start"]), "data"
    if best is None:
        best = quietest_hour(conn, merchant_id, now)
    if best is None:
        best, how = DEFAULT_BEST_TIME, "default"
    prov["best_time"] = how

    # trust: new merchants are new; an open complaint means a strained relationship; otherwise established
    open_complaints = conn.execute("SELECT COUNT(*) FROM tickets WHERE merchant_id = ? AND type = 'COMPLAINT' AND status = 'OPEN'", (merchant_id,)).fetchone()[0]
    if open_complaints:
        trust = "strained"
    elif now - parse_iso(m["registered_at"]) < timedelta(days=NEW_MERCHANT_DAYS):
        trust = "new"
    else:
        trust = "established"
    prov["trust_level"] = "data"

    return ContactProfile(merchant_id, lang, channel, channel in consent, comfort, dm, formality, best, trust, prov)


# ------------------------------------------------------------------------------------------------------- style
@dataclass(frozen=True)
class StyleHint:
    """How to say it. Carries no number, price or term: those live only in OfferFacts."""
    language: str
    channel: str
    format: str                      # voice_note | short_text | email | deck
    tone: str
    formality: str
    max_seconds: int | None          # a spoken length cap; None for text


@dataclass(frozen=True)
class _SegmentStyle:
    name: str
    channel: str
    format: str
    tone: str


SEGMENT_STYLES: dict[str, _SegmentStyle] = {
    "kirana_voice_note": _SegmentStyle("kirana_voice_note", "whatsapp", "voice_note", "warm, respectful"),
    "cafe_text": _SegmentStyle("cafe_text", "whatsapp", "short_text", "friendly, practical"),
    "d2c_email": _SegmentStyle("d2c_email", "email", "email", "crisp, numbers-first"),
    "enterprise_human_led": _SegmentStyle("enterprise_human_led", "human", "deck", "formal"),
}


def segment_style_name(segment: str, profile: ContactProfile) -> str:
    """Which default style fits. Tier-3 kirana and chemist: a voice note, unless the merchant is comfortable with text."""
    if segment in ("kirana", "chemist"):
        return "cafe_text" if profile.digital_comfort == "high" else "kirana_voice_note"
    if segment == "restaurant":
        return "cafe_text"
    if segment == "d2c":
        return "d2c_email"
    if segment == "enterprise":
        return "enterprise_human_led"
    return "cafe_text"


def select_style(profile: ContactProfile, segment: str) -> StyleHint:
    """The segment's default style, then the merchant's explicit preferences on top. The language is always the profile's
    (the merchant's own setting, or what they asked for). Enterprise is human-led: Munim only prepares."""
    base = SEGMENT_STYLES[segment_style_name(segment, profile)]
    channel, fmt = base.channel, base.format
    if base.name != "enterprise_human_led" and profile.provenance.get("preferred_channel") == "explicit":
        channel = profile.preferred_channel
        if channel == "email":
            fmt = "email"
        elif channel in VOICE_CHANNELS:
            fmt = "voice_note"
        elif fmt in ("email", "voice_note"):
            fmt = "short_text"
    spoken = fmt == "voice_note" or channel in VOICE_CHANNELS
    language = "en-IN" if base.name == "enterprise_human_led" else profile.language
    return StyleHint(language, channel, fmt, base.tone, profile.formality if base.name != "enterprise_human_led" else "formal",
                     VOICE_NOTE_SECONDS if spoken else None)


def style_for_merchant(conn: sqlite3.Connection, clock: SimClock, merchant_id: str) -> tuple[ContactProfile, StyleHint]:
    profile = build_contact_profile(conn, clock, merchant_id)
    segment = conn.execute("SELECT segment FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()["segment"]
    return profile, select_style(profile, segment)


# ----------------------------------------------------------------------------------------------------- offer facts
@dataclass(frozen=True)
class OfferFacts:
    """The facts of one offer, built ONLY from system-read tool results, immutable. Whatever style a message takes, these are the
    numbers and names it states. ``items`` is a sorted tuple of (name, value) so two OfferFacts for the same offer compare equal."""
    kind: str                                  # LOAN | FEE_WAIVER | PLAN_CHANGE
    merchant_id: str
    source: str                                # the tool the numbers came from
    items: tuple[tuple[str, int | str], ...]

    def get(self, name: str):
        return dict(self.items)[name]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.kind, self.merchant_id, self.source, list(self.items)], sort_keys=True).encode()).hexdigest()

    @staticmethod
    def loan(eligibility: Mapping[str, Any]) -> "OfferFacts | None":
        """From check_partner_eligibility. Not eligible: None, so nothing about an offer can be said."""
        if not eligibility.get("eligible"):
            return None
        items = {"max_amount_paise": int(eligibility["max_amount_paise"]), "partner": str(eligibility["partner"])}
        if eligibility.get("indicative_terms_ref"):
            items["indicative_terms_ref"] = str(eligibility["indicative_terms_ref"])      # cited from the KB, never computed
        return OfferFacts("LOAN", eligibility["merchant_id"], "check_partner_eligibility", tuple(sorted(items.items())))

    @staticmethod
    def waiver(band: Mapping[str, Any]) -> "OfferFacts":
        """From get_offer_band: the most the pricing system allows for this merchant's segment."""
        return OfferFacts("FEE_WAIVER", band["merchant_id"], "get_offer_band",
                          tuple(sorted({"max_months": int(band["max_months"]), "offer_id": str(band["offer_id"])}.items())))

    @staticmethod
    def plan_change(plan: Mapping[str, Any], to_plan_id: str) -> "OfferFacts":
        """From get_plan: the catalog price of the target plan and the billing date the change takes effect on."""
        catalog = plan["catalog"]
        if to_plan_id not in catalog:
            raise KeyError(to_plan_id)
        return OfferFacts("PLAN_CHANGE", plan["merchant_id"], "get_plan",
                          tuple(sorted({"to_plan_id": to_plan_id, "monthly_fee_paise": int(catalog[to_plan_id]),
                                        "effective_on": str(plan["billing_date"])}.items())))


@dataclass(frozen=True)
class StyledOffer:
    """What a renderer receives: the facts (frozen) and the style to say them in. The renderer cannot change either."""
    facts: OfferFacts
    style: StyleHint
