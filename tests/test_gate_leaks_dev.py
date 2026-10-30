"""BUILD_PLAN A1: grounding leaks and false blocks, reproduced on NEW synthetic dev sources (never the frozen claim set, never its merchants).

Leak shape found by the dev probe: the verifier accepted a claim if ANY one sentence of the cited source entailed it, and on a tiny unrelated
premise ("Merchant city is Mysuru.") the entailment model says ENTAILMENT for "Your KYC is approved.". So a model that cites the wrong read could
send a false status. A sentence may now count only when it is ABOUT the claim (shares a content word); the whole source is unchanged.

False-block shapes: a ticket id read as the bare number 0042; a window that lives only in a field name (replacements_last_30d); a clock time
written "15:00" against a claim that says "3 pm"; the product name the merchant uses ("Soundbox") against the source's "device".
"""
import os

import pytest

from core.gate.sources import Claim, SourceBook, check_claim
from core.gate.verify import GroundingVerifier, about, verbalise

needs_models = pytest.mark.skipif(os.environ.get("MUNIM_SKIP_MODEL_TESTS") == "1", reason="model tests disabled")
ALIASES = {"soundbox": "device"}
# Still blocked after A1: the entailment model stays NEUTRAL (numbers and words now line up). Safe direction: a true reply is held, never a false
# one sent. Listed in docs/LIMITATIONS.md; xfail (not strict) so a model or wording change that fixes them shows up as XPASS.
KNOWN_FALSE_BLOCK = "entailment model stays NEUTRAL on this paraphrase (A1 known false block)"

MERCHANT = {"merchant_id": "M-0906", "name": "Lakshmi Stores", "segment": "kirana", "city": "Mysuru", "status": "ACTIVE", "avg_daily_volume_paise": 189750}
PLAN = {"merchant_id": "M-0906", "plan": "Soundbox Standard", "monthly_fee_paise": 14900, "catalog": {"soundbox_plus": 24900}}
DEVICE_ID_ONLY = {"device_id": "D-0906", "merchant_id": "M-0906"}
SETTLEMENTS = [
    {"settlement_id": "S-0901", "amount_paise": 512000, "status": "PENDING", "expected_at": "2026-11-02T10:00:00+05:30"},
    {"settlement_id": "S-0850", "amount_paise": 230000, "status": "SETTLED", "expected_at": "2026-10-20T10:00:00+05:30"},
]

WRONG_CITATION = [   # a false claim cited to an unrelated read of the same merchant: must never be sendable
    ("tool:get_merchant#1", MERCHANT, "Your KYC is approved."),
    ("tool:get_merchant#1", MERCHANT, "Your bank account is verified."),
    ("tool:get_merchant#1", MERCHANT, "Your Soundbox is online."),
    ("tool:get_plan#1", PLAN, "Your bank account is verified."),
    ("tool:get_device_status#1", DEVICE_ID_ONLY, "Your latest settlement has been paid to your bank."),
]
FALSE_FOR_SOURCE = [  # a false claim cited to the right read: must stay blocked (these already were; kept as regressions)
    ("tool:get_kyc_status#1", {"merchant_id": "M-0906", "status": "REJECTED", "reason": "DOC_BLURRY"}, "Your KYC is approved."),
    ("tool:get_device_status#1", {"device_id": "D-0906", "status": "OFFLINE", "battery": 40}, "Your Soundbox is online."),
    ("tool:get_settlements#1", SETTLEMENTS, "Your latest settlement has been paid to your bank."),
    ("kb:bank_policy#chat@v1", "Munim cannot change bank details in chat. The merchant updates them through a secure link.", "I have changed your bank details."),
    ("kb:scam#screens@v1", "A photo of a payment screen is not proof of payment. Only the merchant's own transaction record is proof.",
     "A photo of a payment screen is proof of payment."),
]
TRUE_FOR_SOURCE = [  # true claims that were blocked
    ("tool:get_tickets#1", [{"ticket_id": "T-0042", "type": "COMPLAINT", "status": "OPEN"}], "Your complaint ticket T-0042 is open."),
    ("tool:get_device_status#1", {"device_id": "D-0907", "status": "ONLINE", "replacements_last_30d": 2}, "Your Soundbox was replaced 2 times in the last 30 days."),
    pytest.param("tool:get_merchant#1", {"merchant_id": "M-0907", "contact_window_start": "15:00", "contact_window_end": "17:00"},
                 "You will be contacted only between 3 pm and 5 pm.", marks=pytest.mark.xfail(reason=KNOWN_FALSE_BLOCK, strict=False)),
    ("tool:get_device_status#1", {"device_id": "D-0906", "status": "OFFLINE", "battery": 40}, "Your Soundbox is offline right now."),
    pytest.param("kb:replace#fault@v1", "A replacement for a device fault is free of charge. Damage caused by water is reviewed by a person.",
                 "A Soundbox replacement for a device fault is free of charge.", marks=pytest.mark.xfail(reason=KNOWN_FALSE_BLOCK, strict=False)),
    ("tool:get_settlements#1", SETTLEMENTS, "Your pending settlement is 5,120 rupees."),
]


def book(sid, body):
    b = SourceBook()
    (b.add_tool_result if sid.startswith("tool:") else b.add_kb)(sid, body)
    return b


# ---------------------------------------------------------------------------------------------- no model needed
def test_an_identifier_in_a_claim_must_appear_verbatim_in_the_source():
    b = book("tool:get_tickets#1", [{"ticket_id": "T-0042", "status": "OPEN"}])
    assert check_claim(Claim("c", "Your ticket T-0042 is open.", "tool:get_tickets#1"), b).ok
    bad = check_claim(Claim("c", "Your ticket T-0099 is open.", "tool:get_tickets#1"), b)
    assert not bad.ok and bad.unmatched_numbers == ["T-0099"]


def test_identifier_digits_never_count_as_a_quantity():
    b = book("tool:get_tickets#1", [{"ticket_id": "T-0042", "status": "OPEN"}])
    bad = check_claim(Claim("c", "You owe 42 rupees on ticket T-0042.", "tool:get_tickets#1"), b)
    assert not bad.ok and bad.unmatched_numbers == ["42"]             # the id is matched; its digits do not make "42 rupees" true


def test_a_number_in_a_field_name_is_available_to_the_claim():
    b = book("tool:get_device_status#1", {"replacements_last_30d": 2})
    assert check_claim(Claim("c", "It was replaced 2 times in the last 30 days.", "tool:get_device_status#1"), b).ok
    assert not check_claim(Claim("c", "It was replaced 2 times in the last 60 days.", "tool:get_device_status#1"), b).ok


def test_verbalised_sources_spell_out_windows_and_clock_times():
    s = verbalise("tool:get_merchant#1", {"contact_window_start": "15:00", "replacements_last_30d": 2})
    assert "15:00 (3 pm)" in s and "last 30 days" in s


@pytest.mark.parametrize("sentence, claim, related", [
    ("Merchant city is Mysuru.", "Your KYC is approved.", False),
    ("Plan catalog soundbox plus is 24900.", "Your bank account is verified.", False),
    ("Device merchant id is M-0906.", "Your latest settlement has been paid to your bank.", False),
    ("Settlements item 1 amount is 5120 rupees.", "Your pending settlement is 5,120 rupees.", True),
    ("Kyc status is REJECTED.", "Your KYC was rejected.", True),
    ("Device status is OFFLINE.", "Your Soundbox is offline right now.", True),      # through the alias soundbox -> device
])
def test_a_sentence_counts_only_when_it_is_about_the_claim(sentence, claim, related):
    assert about(sentence, claim, ALIASES) is related


# ---------------------------------------------------------------------------------------------- the real entailment model
@pytest.fixture(scope="module")
def verifier():
    from core.gate.nli import EntailmentModel
    return GroundingVerifier(EntailmentModel(), aliases=ALIASES)


@needs_models
@pytest.mark.parametrize("sid, body, text", WRONG_CITATION + FALSE_FOR_SOURCE, ids=lambda x: x if isinstance(x, str) and " " in x else "")
def test_false_claims_are_never_sendable(verifier, sid, body, text):
    v = verifier.verify([Claim("c", text, sid)], book(sid, body))[0]
    assert not v.sendable, (text, sid, v.reason)


@needs_models
@pytest.mark.parametrize("sid, body, text", TRUE_FOR_SOURCE, ids=lambda x: x if isinstance(x, str) and " " in x else "")
def test_true_claims_are_sendable(verifier, sid, body, text):
    v = verifier.verify([Claim("c", text, sid)], book(sid, body))[0]
    assert v.sendable, (text, sid, v.reason)


# ---------------------------------------------------------------------------------------------- found live 2026-10-30 (C1): a 100x amount
@pytest.mark.parametrize("value", [512000, "512000"])
def test_a_paise_field_offers_only_its_rupee_value(value):
    """The model said "eligible up to Rs 110000000" for max_amount_paise 110000000 (= Rs 11,00,000) and the number check let it through, because a
    paise field's raw integer was also offered as a number. Merchants are told rupees, never paise, so only the rupee value is available."""
    b = book("tool:get_settlements#1", {"amount_paise": value})
    assert check_claim(Claim("c", "Your settlement is Rs 5,120.", "tool:get_settlements#1"), b).ok
    hundred_x = check_claim(Claim("c", "Your settlement is Rs 512000.", "tool:get_settlements#1"), b)
    assert not hundred_x.ok and hundred_x.unmatched_numbers == ["512000"]
