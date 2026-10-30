"""#76: the contact profile is built from data, explicit preferences win, style varies and facts do not."""
import dataclasses
import sqlite3
from datetime import timedelta

import pytest

from core.engine.personalisation import (OfferFacts, SEGMENT_STYLES, StyledOffer, StyleHint, build_contact_profile, quietest_hour,
                                         select_style, style_for_merchant)
from core.memory.profile import ProfileStore
from packs.paytm_merchant.actions import build_registry
from tests.world import World

KIRANA_KN, CAFE_HINGLISH, D2C_EN = "M-0001", "M-0003", "M-0009"       # Hubballi kirana, Bengaluru restaurant, Bengaluru D2C


def profile(conn, clock, mid):
    return build_contact_profile(conn, clock, mid)


# ------------------------------------------------------------------------------------------------ from data, not names
def test_language_comes_from_the_merchants_own_setting_and_follows_it(conn, clock):
    assert profile(conn, clock, KIRANA_KN).language == "kn-IN"
    conn.execute("UPDATE merchants SET language='hinglish' WHERE merchant_id=?", (KIRANA_KN,))
    assert profile(conn, clock, KIRANA_KN).language == "hinglish"


def test_region_is_not_used_to_guess_a_language(conn, clock):
    """A Hubballi merchant whose app is set to English is spoken to in English; the city changes nothing."""
    conn.execute("UPDATE merchants SET language='en-IN' WHERE merchant_id=?", (KIRANA_KN,))
    before = profile(conn, clock, KIRANA_KN)
    conn.execute("UPDATE merchants SET city='Pune', pincode='411001', lat=18.5, lng=73.8 WHERE merchant_id=?", (KIRANA_KN,))
    assert before.language == "en-IN" == profile(conn, clock, KIRANA_KN).language


def test_the_name_is_never_read_and_never_changes_the_profile(conn, clock):
    reads = []
    conn.set_authorizer(lambda action, a1, a2, db, src: (reads.append((a1, a2)) if action == sqlite3.SQLITE_READ else None) or sqlite3.SQLITE_OK)
    base = profile(conn, clock, KIRANA_KN)
    conn.set_authorizer(None)
    assert ("merchants", "name") not in reads
    assert ("merchants", "language") in reads                     # the authorizer works: it saw the columns that ARE used
    conn.execute("UPDATE merchants SET name='Zainab Fatima Provisions' WHERE merchant_id=?", (KIRANA_KN,))
    assert dataclasses.replace(profile(conn, clock, KIRANA_KN), provenance={}) == dataclasses.replace(base, provenance={})
    conn.execute("UPDATE merchants SET name='Ramesh Iyer & Sons' WHERE merchant_id=?", (KIRANA_KN,))
    assert profile(conn, clock, KIRANA_KN) == base


def test_a_fact_about_a_persons_community_is_ignored_even_if_it_is_in_the_table(conn, clock):
    """The store refuses these keys; if one ever got in by another route, the profile still must not read it."""
    base = profile(conn, clock, KIRANA_KN)
    for key, value in (("community", "x"), ("caste", "y"), ("religion", "z"), ("surname_region", "hi-IN")):
        conn.execute("INSERT INTO profile_facts(merchant_id,key,value,source,confidence,valid_from) VALUES (?,?,?,?,1.0,?)",
                     (KIRANA_KN, key, value, "SYSTEM", clock.now_iso()))
    conn.execute("INSERT INTO profile_facts(merchant_id,key,value,source,confidence,valid_from) VALUES (?,?,?,?,1.0,?)",
                 (KIRANA_KN, "language", "hi-IN", "AGENT_INFERRED", clock.now_iso()))     # inferred language never beats the merchant's own setting
    assert profile(conn, clock, KIRANA_KN) == base


# --------------------------------------------------------------------------------------------- explicit preferences win
def test_an_explicit_language_beats_the_setting(conn, clock):
    ProfileStore(conn, clock).set_fact(KIRANA_KN, "language", "en", "MERCHANT_SAID")
    p = profile(conn, clock, KIRANA_KN)
    assert p.language == "en-IN" and p.provenance["language"] == "explicit"


def test_an_explicit_channel_beats_replies_and_defaults_and_flags_missing_consent(conn, clock):
    for _ in range(5):
        conn.execute("INSERT INTO contact_touches(subject_id, channel, case_id, direction, ts) VALUES (?,?,?,?,?)", (KIRANA_KN, "app", None, "IN", clock.now_iso()))
    assert profile(conn, clock, KIRANA_KN).preferred_channel == "app"                       # what they actually reply on
    ProfileStore(conn, clock).set_fact(KIRANA_KN, "preferred_channel", "sms", "MERCHANT_SAID")
    p = profile(conn, clock, KIRANA_KN)
    assert p.preferred_channel == "sms" and p.provenance["preferred_channel"] == "explicit"
    assert p.channel_consented is False                                                      # asked for, never consented: the policy still blocks a send


def test_a_stated_contact_window_beats_the_payment_curve(conn, clock):
    assert quietest_hour(conn, KIRANA_KN, clock.now()) is not None
    ProfileStore(conn, clock).set_fact(KIRANA_KN, "contact_window", "21:30-22:30", "MERCHANT_SAID")
    p = profile(conn, clock, KIRANA_KN)
    assert p.best_time == "21:30" and p.provenance["best_time"] == "explicit"


def test_a_window_the_merchant_set_on_their_account_is_used_when_nothing_is_said(conn, clock):
    p = profile(conn, clock, "M-0003")                  # seeded with a 15:00-17:00 contact window
    assert p.best_time == "15:00" and p.provenance["best_time"] == "data"


def test_best_time_is_the_quietest_hour_of_the_payment_curve(conn, clock):
    conn.execute("DELETE FROM payments WHERE merchant_id=?", (KIRANA_KN,))
    day = clock.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    pid = 0
    for hour in range(9, 21):
        for _ in range(3 if hour != 15 else 1):          # every hour gets 3 payments, except 15:00 which gets 1
            pid += 1
            conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?)", (f"PX-{pid}", KIRANA_KN, 10000, "SUCCESS", (day + timedelta(hours=hour, minutes=pid % 50)).isoformat(), f"U{pid}", None))
    assert quietest_hour(conn, KIRANA_KN, clock.now()) == "15:00"
    assert profile(conn, clock, KIRANA_KN).best_time == "15:00"


def test_too_little_payment_history_gives_the_default_not_a_guess(conn, clock):
    conn.execute("DELETE FROM payments WHERE merchant_id=?", (KIRANA_KN,))
    assert quietest_hour(conn, KIRANA_KN, clock.now()) is None
    p = profile(conn, clock, KIRANA_KN)
    assert p.best_time == "11:00" and p.provenance["best_time"] == "default"


def test_explicit_formality_and_decision_maker_win_over_the_segment_default(conn, clock):
    assert profile(conn, clock, KIRANA_KN).formality == "warm"
    ps = ProfileStore(conn, clock)
    ps.set_fact(KIRANA_KN, "formality", "formal", "MERCHANT_SAID")
    ps.set_fact(KIRANA_KN, "decision_maker", "owner_and_accountant", "HUMAN_AGENT")
    p = profile(conn, clock, KIRANA_KN)
    assert (p.formality, p.decision_maker) == ("formal", "owner_and_accountant")


def test_trust_level_is_new_established_or_strained(conn, clock):
    assert profile(conn, clock, KIRANA_KN).trust_level == "established"
    assert profile(conn, clock, "M-0006").trust_level == "new"                              # registered 4 days ago
    assert profile(conn, clock, "M-0002").trust_level == "strained"                         # an open complaint


def test_digital_comfort_follows_how_the_merchant_replies(conn, clock):
    assert profile(conn, clock, KIRANA_KN).digital_comfort == "medium"
    conn.execute("INSERT INTO contact_touches(subject_id, channel, case_id, direction, ts) VALUES (?,?,?,?,?)", (KIRANA_KN, "voice", None, "IN", clock.now_iso()))
    assert profile(conn, clock, KIRANA_KN).digital_comfort == "low"
    for _ in range(4):
        conn.execute("INSERT INTO contact_touches(subject_id, channel, case_id, direction, ts) VALUES (?,?,?,?,?)", ("M-0004", "app", None, "IN", clock.now_iso()))
    assert profile(conn, clock, "M-0004").digital_comfort == "high"


# ---------------------------------------------------------------------------------------------------- segment styles
def test_the_four_segment_styles_have_their_documented_shape(conn, clock):
    _, kirana = style_for_merchant(conn, clock, KIRANA_KN)
    _, cafe = style_for_merchant(conn, clock, CAFE_HINGLISH)
    _, d2c = style_for_merchant(conn, clock, D2C_EN)
    assert kirana == StyleHint("kn-IN", "whatsapp", "voice_note", "warm, respectful", "warm", 20)
    assert cafe == StyleHint("hinglish", "whatsapp", "short_text", "friendly, practical", "friendly", None)
    assert d2c == StyleHint("en-IN", "email", "email", "crisp, numbers-first", "professional", None)
    ent = select_style(profile(conn, clock, D2C_EN), "enterprise")
    assert (ent.channel, ent.format, ent.formality, ent.language) == ("human", "deck", "formal", "en-IN")     # human-led: Munim only prepares
    assert set(SEGMENT_STYLES) == {"kirana_voice_note", "cafe_text", "d2c_email", "enterprise_human_led"}


def test_a_kirana_who_is_comfortable_with_text_gets_text_not_a_voice_note(conn, clock):
    ProfileStore(conn, clock).set_fact(KIRANA_KN, "digital_comfort_text", "true", "MERCHANT_SAID")
    _, s = style_for_merchant(conn, clock, KIRANA_KN)
    assert (s.format, s.max_seconds, s.language) == ("short_text", None, "kn-IN")


def test_explicit_channel_and_language_win_over_the_segment_style(conn, clock):
    ps = ProfileStore(conn, clock)
    ps.set_fact(D2C_EN, "preferred_channel", "voice", "MERCHANT_SAID")
    ps.set_fact(D2C_EN, "language", "hi", "MERCHANT_SAID")
    _, s = style_for_merchant(conn, clock, D2C_EN)
    assert (s.channel, s.format, s.language, s.max_seconds) == ("voice", "voice_note", "hi-IN", 20)


def test_style_hint_has_no_money_or_term_field():
    assert {f.name for f in dataclasses.fields(StyleHint)} == {"language", "channel", "format", "tone", "formality", "max_seconds"}


# ----------------------------------------------------------------------------------------- facts do not vary with style
def three_profiles(conn, clock):
    return [style_for_merchant(conn, clock, mid) for mid in (KIRANA_KN, CAFE_HINGLISH, D2C_EN)]


def test_the_same_offer_for_three_profiles_has_identical_facts_and_different_style(db_path, clock):
    w = World(db_path, clock, {})
    band = w.reader("get_offer_band", {"merchant_id": KIRANA_KN})
    facts = OfferFacts.waiver(band)                                      # ONE set of facts, from the pricing system
    offers = [StyledOffer(facts, style) for _, style in three_profiles(w.conn, clock)]
    assert len({o.facts.fingerprint for o in offers}) == 1 and offers[0].facts is offers[1].facts is offers[2].facts
    assert facts.get("max_months") == 1 and facts.source == "get_offer_band"
    styles = [o.style for o in offers]
    assert len({(s.language, s.channel, s.format, s.tone) for s in styles}) == 3             # three different voices
    assert [s.language for s in styles] == ["kn-IN", "hinglish", "en-IN"]
    assert [s.format for s in styles] == ["voice_note", "short_text", "email"]


def test_loan_facts_come_from_the_tool_and_are_none_when_not_eligible(db_path, clock):
    w = World(db_path, clock, {})
    el = w.reader("check_partner_eligibility", {"merchant_id": CAFE_HINGLISH})
    facts = OfferFacts.loan(el)
    assert facts.get("max_amount_paise") == el["max_amount_paise"] == 110_000_000
    assert facts.get("partner") == "Demo Partner NBFC" and facts.get("indicative_terms_ref") == el["indicative_terms_ref"]
    assert not any(k in dict(facts.items) for k in ("rate", "tenure_months", "interest", "emi"))      # nothing the tool did not return
    assert OfferFacts.loan(w.reader("check_partner_eligibility", {"merchant_id": KIRANA_KN})) is None  # ineligible: no offer exists to mention


def test_offer_facts_are_immutable(db_path, clock):
    w = World(db_path, clock, {})
    facts = OfferFacts.waiver(w.reader("get_offer_band", {"merchant_id": KIRANA_KN}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.items = (("max_months", 12),)
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.kind = "LOAN"
    assert isinstance(facts.items, tuple) and all(isinstance(i, tuple) for i in facts.items)
    with pytest.raises(dataclasses.FrozenInstanceError):
        StyledOffer(facts, select_style(profile(w.conn, clock, KIRANA_KN), "kirana")).style = None


def test_offer_facts_track_the_pricing_system_not_the_style(db_path, clock):
    w = World(db_path, clock, {})
    a = OfferFacts.waiver(w.reader("get_offer_band", {"merchant_id": KIRANA_KN}))
    w.conn.execute("UPDATE offer_bands SET max_months=3 WHERE segment='kirana'")
    b = OfferFacts.waiver(w.reader("get_offer_band", {"merchant_id": KIRANA_KN}))
    assert (a.get("max_months"), b.get("max_months")) == (1, 3) and a.fingerprint != b.fingerprint


def test_plan_change_facts_use_the_catalog_price_and_billing_date(db_path, clock):
    w = World(db_path, clock, {})
    plan = w.reader("get_plan", {"merchant_id": D2C_EN})
    f = OfferFacts.plan_change(plan, "SOUNDBOX_STD")
    assert (f.get("monthly_fee_paise"), f.get("effective_on")) == (14900, "2026-11-03")
    with pytest.raises(KeyError):
        OfferFacts.plan_change(plan, "PLATINUM_FREE")


def test_building_a_profile_never_writes(conn, clock):
    before = conn.total_changes
    build_contact_profile(conn, clock, KIRANA_KN)
    assert conn.total_changes == before
