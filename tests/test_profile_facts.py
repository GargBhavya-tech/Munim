from datetime import timedelta

import pytest

from core.engine.clock import IST
from core.memory.profile import ConsentWithdrawn, ProfileError, ProfileStore, check_key

M = "M-0001"


@pytest.fixture
def ps(conn, clock):
    return ProfileStore(conn, clock)


def test_supersede_closes_old_row_and_keeps_history(ps, clock, conn):
    first = ps.set_fact(M, "language", "hi", "MERCHANT_SAID", "T-1")
    t1 = clock.now_iso()
    clock.advance(timedelta(hours=1))
    ps.set_fact(M, "language", "kn", "MERCHANT_SAID", "T-2")
    hist = ps.history(M, "language")
    assert [(h.value, h.valid_to) for h in hist] == [("hi", clock.now_iso()), ("kn", None)]
    assert hist[0].fact_id == first.fact_id and hist[0].valid_from == t1  # old row kept, only valid_to closed
    assert ps.get(M, "language").value == "kn"
    assert conn.execute("SELECT COUNT(*) FROM profile_facts WHERE merchant_id=? AND key='language'", (M,)).fetchone()[0] == 2


def test_same_value_does_not_add_history(ps):
    a = ps.set_fact(M, "shop_closed_day", "tuesday", "MERCHANT_SAID")
    b = ps.set_fact(M, "shop_closed_day", "tuesday", "MERCHANT_SAID")
    assert a.fact_id == b.fact_id and len(ps.history(M, "shop_closed_day")) == 1


def test_kyc_claim_is_merchant_said_and_never_unlocks_action(ps, conn, clock):
    ps.set_fact(M, "kyc_status", "PENDING", "SYSTEM", "kyc:read")
    ps.set_fact(M, "kyc_claim", "done", "MERCHANT_SAID", "T-9")  # "Mera KYC ho gaya hai"
    assert ps.get(M, "kyc_claim").source == "MERCHANT_SAID"
    assert ps.facts_for_action(M) == {"kyc_status": ps.get(M, "kyc_status")}
    assert "kyc_claim" not in ps.facts_for_action(M)
    # a claim with the SAME key as a system fact must neither replace nor close the system row
    clock.advance(timedelta(minutes=1))
    ps.set_fact(M, "kyc_status", "VERIFIED", "MERCHANT_SAID", "T-10")
    assert ps.facts_for_action(M)["kyc_status"].value == "PENDING"
    assert ps.get(M, "kyc_status").value == "PENDING"  # SYSTEM outranks the claim
    assert conn.execute("SELECT kyc.status FROM kyc WHERE merchant_id=?", (M,)).fetchone() is not None  # tables untouched by memory


def test_only_system_in_facts_for_action_and_pricing(ps):
    ps.set_fact(M, "plan", "basic", "SYSTEM")
    ps.set_fact(M, "note", "x", "HUMAN_AGENT")
    ps.set_fact(M, "prefers_call", "yes", "AGENT_INFERRED", confidence=0.4)
    assert set(ps.facts_for_action(M)) == {"plan"}
    assert set(ps.facts_for_pricing_or_targeting(M)) == {"plan"}


@pytest.mark.parametrize("key", ["religion", "caste", "community", "Merchant_Religion", "caste-group", "ethnicity", "political_view"])
def test_protected_trait_keys_rejected_for_every_source(ps, key):
    for src in ("AGENT_INFERRED", "MERCHANT_SAID", "HUMAN_AGENT", "SYSTEM"):
        with pytest.raises(ProfileError, match="protected"):
            ps.set_fact(M, key, "x", src)
    assert ps.history(M) == []


def test_inferred_only_from_fixed_tag_list_and_marked_inferred(ps):
    with pytest.raises(ProfileError, match="fixed inferred-tag list"):
        ps.set_fact(M, "will_buy_loan", "yes", "AGENT_INFERRED")
    f = ps.set_fact(M, "digital_comfort_voice", "high", "AGENT_INFERRED", confidence=0.6)
    assert f.inferred and f.confidence == 0.6
    assert not ps.set_fact(M, "plan", "x", "SYSTEM").inferred


def test_explicit_preference_beats_inference_even_if_inference_is_newer(ps, clock):
    ps.set_fact(M, "language", "en", "MERCHANT_SAID", "T-1")  # "English please"
    clock.advance(timedelta(minutes=5))
    ps.set_fact(M, "language", "hi", "AGENT_INFERRED", confidence=0.9)  # detection says Hindi
    assert ps.get(M, "language").value == "en"
    clock.advance(timedelta(minutes=5))
    ps.set_fact(M, "language", "kn", "MERCHANT_SAID", "T-2")  # a later explicit change wins
    assert ps.get(M, "language").value == "kn"


def test_declined_applies_in_every_case_until_expiry(ps, clock):
    ps.decline(M, "S3", clock.now() + timedelta(days=30))
    assert ps.is_declined(M, "S3") and not ps.is_declined(M, "S4") and not ps.is_declined("M-0002", "S3")
    clock.advance(timedelta(days=31))
    assert not ps.is_declined(M, "S3")


def test_do_not_contact_helper(ps):
    assert not ps.is_do_not_contact(M)
    ps.set_do_not_contact(M)
    assert ps.is_do_not_contact(M) and not ps.is_do_not_contact("M-0002")


@pytest.mark.parametrize("val", ["my otp is 482913", "PIN: 4321", "4111 1111 1111 1111", "card 4111-1111-1111-1111 ok", "password hunter2222"])
def test_secret_values_rejected(ps, val):
    with pytest.raises(ProfileError, match="never stored"):
        ps.set_fact(M, "note", val, "MERCHANT_SAID")
    assert ps.history(M) == []


@pytest.mark.parametrize("key", ["otp", "upi_pin", "card_number", "cvv"])
def test_secret_keys_rejected(ps, key):
    with pytest.raises(ProfileError, match="never stored"):
        ps.set_fact(M, key, "x", "MERCHANT_SAID")


def test_harmless_keys_and_values_still_work(ps):
    ps.set_fact(M, "shopping_area", "mapping road", "MERCHANT_SAID")  # contains 'pin' only inside words
    ps.set_fact(M, "phone_last4", "4321", "SYSTEM")
    ps.set_fact(M, "avg_ticket_paise", "250000", "SYSTEM")
    assert ps.get(M, "phone_last4").value == "4321"
    assert check_key("Call_After_9pm") == "call_after_9pm"


def test_withdrawn_consent_stops_use_of_scoped_facts(ps, conn, clock):
    ps.set_fact(M, "prefers_whatsapp", "yes", "MERCHANT_SAID", consent_scope="whatsapp")
    ps.set_fact(M, "shop_hours", "10-8", "MERCHANT_SAID")
    ps.set_do_not_contact(M)
    conn.execute("INSERT OR REPLACE INTO consent(subject_id,channel,status,source,ts) VALUES (?,?,?,?,?)", (M, "whatsapp", "REVOKED", "t", clock.now_iso()))
    assert set(ps.current(M)) == {"shop_hours", "do_not_contact"}
    conn.execute("INSERT OR REPLACE INTO consent(subject_id,channel,status,source,ts) VALUES (?,?,?,?,?)", (M, "memory", "REVOKED", "t", clock.now_iso()))
    assert set(ps.current(M)) == {"do_not_contact"}  # restrictive facts stay in force
    with pytest.raises(ConsentWithdrawn):
        ps.set_fact(M, "shop_hours", "9-9", "MERCHANT_SAID")
