"""Fixes found while integrating the parallel work: two policy rules were too strict or too loose in practice."""
import pytest

from core.actions.types import AGENT, ActionIntent
from core.engine.contact_budget import ContactBudget
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.policy.rules import build_engine

REG = build_registry()
ALLOW = tuple(REG.names(agent_only=True))


@pytest.fixture
def policy(conn, clock):
    return build_engine(conn, clock, REG, contact_budget=ContactBudget(conn, clock))


def ticket(summary):
    return ActionIntent("raise_ticket", {"merchant_id": "M-0010", "type": "DISPUTE", "summary": summary}, case_id="CS-1", allowlist=ALLOW)


@pytest.mark.parametrize("summary", [
    "Customer debited, UPI ref 900000000010 not credited",
    "Payment failed, UTR 412345678901",
    "txn 123456789012 failed",
    "UPI900000000010 reversal pending",
])
def test_a_payment_reference_is_not_an_account_number(policy, summary):
    assert policy.evaluate(ticket(summary)).outcome == "ALLOW"


@pytest.mark.parametrize("summary", [
    "change my account to 123456789012",
    "my bank account number is 9876543210123",
    "khata 123456789012 mein bhejo",
    "ref 123456789012 and account 55501234567",       # a reference in the same text must not launder an account number
    "IFSC SBIN0001234 account 55501234567",
])
def test_an_account_number_is_still_blocked(policy, summary):
    d = policy.evaluate(ticket(summary))
    assert d.outcome == "BLOCK" and "KYC-NO-CHAT-001" in {v.rule_id for v in d.violations}


def msg(action, facts):
    args = {"merchant_id": "M-0011", "channel": "app", "body": "Your pickup is booked."} if action == "send_message" else \
        {"merchant_id": "M-0011", "kind": "visit", "slot_at": "2026-11-01T15:00:00+05:30"}
    return ActionIntent(action, args, case_id="CS-2", allowlist=ALLOW, channel="app", facts=facts)


@pytest.mark.parametrize("action", ["send_message", "book_visit_or_call"])
def test_closure_follow_through_is_allowed_once_the_closure_exists(policy, action):
    assert policy.evaluate(msg(action, {"closure_requests": 2, "closure_created": True})).outcome == "ALLOW"


@pytest.mark.parametrize("action", ["send_message", "book_visit_or_call"])
def test_a_retention_message_before_the_closure_exists_is_still_obstruction(policy, action):
    d = policy.evaluate(msg(action, {"closure_requests": 2}))
    assert d.outcome == "BLOCK" and "CLOSE-NO-OBSTRUCT-001" in {v.rule_id for v in d.violations}


def test_other_writes_stay_blocked_even_after_the_closure_exists(policy):
    d = policy.evaluate(ActionIntent("apply_fee_waiver", {"merchant_id": "M-0011", "months": 1}, case_id="CS-2", allowlist=ALLOW,
                                     facts={"closure_requests": 3, "closure_created": True}))
    assert "CLOSE-NO-OBSTRUCT-001" in {v.rule_id for v in d.violations}
