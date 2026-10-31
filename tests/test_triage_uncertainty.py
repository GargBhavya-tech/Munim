import pytest

from core.engine.intents import INTENT_TO_PLAYBOOK as MAP
from core.engine.triage import IntentRequest, UNSUPPORTED_PLAYBOOK, triage_message


def test_two_requests_example_soundbox_and_loan():
    """'Mera Soundbox band hai aur loan ka bhi batao': two requests, uncertainty 0.2, no clarification."""
    r = triage_message("Mera Soundbox band hai aur loan ka bhi batao",
                       [("REPORT_DEVICE_PROBLEM", 0.80), ("LOAN_INTEREST", 0.60)], "REPORT_DEVICE_PROBLEM", MAP)
    assert r.clarify is False and r.clarify_between is None
    assert r.uncertainty == 0.2
    assert r.requests == [IntentRequest("REPORT_DEVICE_PROBLEM", "C2_SOUNDBOX_FAULT", 0.80),
                          IntentRequest("LOAN_INTEREST", "S3_LOAN", 0.60)]


def test_close_rivals_example_paisa_nahi_aaya_asks_one_question_and_takes_no_action():
    r = triage_message("paisa nahi aaya", [("SETTLEMENT_MISSING", 0.48), ("CUSTOMER_PAYMENT_FAILED", 0.42)], None, MAP)
    assert r.clarify is True
    assert r.uncertainty == 0.94
    assert r.requests == []                                    # no case opens, no action
    assert r.clarify_between == ("SETTLEMENT_MISSING", "CUSTOMER_PAYMENT_FAILED")


def test_watch_out_the_two_examples_are_not_swapped():
    multi = triage_message("", [("REPORT_DEVICE_PROBLEM", 0.80), ("LOAN_INTEREST", 0.60)], "REPORT_DEVICE_PROBLEM", MAP)
    close = triage_message("", [("SETTLEMENT_MISSING", 0.48), ("CUSTOMER_PAYMENT_FAILED", 0.42)], "SETTLEMENT_MISSING", MAP)
    assert not multi.clarify and len(multi.requests) == 2        # not treated as ambiguous
    assert close.clarify and close.requests == []                # not treated as clear


def test_rule_model_disagreement_adds_point_three():
    agree = triage_message("", [("SETTLEMENT_MISSING", 0.9)], "SETTLEMENT_MISSING", MAP)
    differ = triage_message("", [("SETTLEMENT_MISSING", 0.9)], "COMPLAINT", MAP)
    assert agree.uncertainty == pytest.approx(0.1)
    assert differ.uncertainty == pytest.approx(0.4)
    assert triage_message("", [("SETTLEMENT_MISSING", 0.9)], None, MAP).uncertainty == pytest.approx(0.1)   # no rule signal: no penalty


def test_uncertainty_is_capped_at_one():
    r = triage_message("", [("SETTLEMENT_MISSING", 0.30), ("CUSTOMER_PAYMENT_FAILED", 0.29)], "LOAN_INTEREST", MAP)
    assert r.uncertainty == 1.0                                # 1 - 0.01 + 0.3 -> capped
    assert r.clarify


def test_gap_exactly_point_two_is_not_a_close_rivalry():
    r = triage_message("", [("SETTLEMENT_MISSING", 0.45), ("CUSTOMER_PAYMENT_FAILED", 0.25)], None, MAP)
    assert r.clarify is False
    assert [q.intent for q in r.requests] == ["SETTLEMENT_MISSING"]
    assert r.uncertainty == pytest.approx(0.8)                 # rival 0.25 competes: 1 - (0.45 - 0.25)


def test_rival_just_under_the_gap_clarifies():
    r = triage_message("", [("SETTLEMENT_MISSING", 0.45), ("CUSTOMER_PAYMENT_FAILED", 0.26)], None, MAP)
    assert r.clarify and r.clarify_between == ("SETTLEMENT_MISSING", "CUSTOMER_PAYMENT_FAILED")


def test_strong_top_with_a_weak_rival_does_not_clarify_but_counts_the_rival_in_uncertainty():
    r = triage_message("", [("SETTLEMENT_MISSING", 0.9), ("CUSTOMER_PAYMENT_FAILED", 0.4)], "SETTLEMENT_MISSING", MAP)
    assert not r.clarify and [q.intent for q in r.requests] == ["SETTLEMENT_MISSING"]
    assert r.uncertainty == pytest.approx(0.5)


def test_strong_top_with_a_close_but_not_present_rival_clarifies():
    r = triage_message("", [("SETTLEMENT_MISSING", 0.55), ("CUSTOMER_PAYMENT_FAILED", 0.45)], None, MAP)
    assert r.clarify and r.requests == []


def test_two_strong_intents_close_in_weight_are_still_two_requests():
    r = triage_message("", [("REPORT_DEVICE_PROBLEM", 0.85), ("SETTLEMENT_MISSING", 0.80)], "REPORT_DEVICE_PROBLEM", MAP)
    assert not r.clarify and [q.playbook_id for q in r.requests] == ["C2_SOUNDBOX_FAULT", "C1_SETTLEMENT_DELAY"]


def test_two_intents_of_the_same_playbook_are_one_request_and_never_rivals():
    r = triage_message("", [("REPORT_DEVICE_PROBLEM", 0.45), ("NO_PAYMENT_ANNOUNCEMENT", 0.44)], "REPORT_DEVICE_PROBLEM", MAP)
    assert not r.clarify
    assert r.requests == [IntentRequest("REPORT_DEVICE_PROBLEM", "C2_SOUNDBOX_FAULT", 0.45)]
    assert r.uncertainty == pytest.approx(0.55)


def test_unmapped_intent_goes_to_x0():
    r = triage_message("book me a train ticket", [("BOOK_TRAIN", 0.9)], None, MAP)
    assert r.requests == [IntentRequest("BOOK_TRAIN", UNSUPPORTED_PLAYBOOK, 0.9)]
    assert not r.clarify


def test_model_returns_nothing_rule_only():
    r = triage_message("loan", [], "LOAN_INTEREST", MAP)
    assert [(q.intent, q.playbook_id) for q in r.requests] == [("LOAN_INTEREST", "S3_LOAN")]
    assert not r.clarify and r.uncertainty == 0.5


def test_nothing_at_all_asks_what_the_merchant_needs():
    r = triage_message("hmm", [], None, MAP)
    assert r.clarify and r.requests == [] and r.uncertainty == 1.0 and r.clarify_between is None


def test_input_order_does_not_matter():
    a = triage_message("", [("LOAN_INTEREST", 0.6), ("REPORT_DEVICE_PROBLEM", 0.8)], "REPORT_DEVICE_PROBLEM", MAP)
    b = triage_message("", [("REPORT_DEVICE_PROBLEM", 0.8), ("LOAN_INTEREST", 0.6)], "REPORT_DEVICE_PROBLEM", MAP)
    assert a == b
