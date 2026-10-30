"""#121 mock mandate service. E7: three payments summing above the limit, the third escalates (policy first, the mock as a second wall)."""
from datetime import timedelta

import pytest
from pydantic import BaseModel

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass
from core.actions.types import ActionIntent, Principal
from packs.paytm_merchant.actions import SPECS
from packs.paytm_merchant.mocks import mandate
from packs.paytm_merchant.policy.rules import build_engine
from tests.conftest import count

M = "M-0042"
LIMIT = 500_000


class Pay(BaseModel):
    merchant_id: str
    amount_paise: int


@pytest.fixture
def mm(app, mock):
    app.include_router(mandate.router)
    assert mock.write("POST", "/mock/mandate", key="set-1", json={"merchant_id": M, "monthly_limit_paise": LIMIT}).status == 201
    return mock


def facts_from_mock(mock):
    m = mock.get("/mock/mandate", mid=M).body["mandate"]
    return {"mandate_limit_paise": m["monthly_limit_paise"], "mandate_month_total_paise": m["month_total_paise"], "mandate_purpose": m["purpose"]}


def pay(mock, key, amount, purpose="paytm_dues"):
    return mock.write("POST", "/mock/mandate/payments", key=key, json={"merchant_id": M, "amount_paise": amount, "purpose": purpose})


def decide(engine, mock, amount):
    return engine.evaluate(ActionIntent("collect_mandate_payment", {"merchant_id": M, "amount_paise": amount}, principal=Principal("AGENT", "munim"),
                                        allowlist=("collect_mandate_payment",), facts=facts_from_mock(mock)))


@pytest.fixture
def engine(conn, clock):
    reg = ActionRegistry([*SPECS, ActionSpec("collect_mandate_payment", RiskClass.R3, Pay, description="test-only mandate action")])
    return build_engine(conn, clock, reg)


def test_e7_three_payments_over_the_limit_the_third_escalates(mm, engine):
    """Limit 5000.00; payments of 2000.00, 2000.00 then 2000.00: each alone is under the limit, the running total is not."""
    for i in (1, 2):
        d = decide(engine, mm, 200_000)
        assert not [v for v in d.violations if v.rule_id == "MANDATE-LIMIT-001"]
        assert pay(mm, f"p{i}", 200_000).status == 201
    assert facts_from_mock(mm)["mandate_month_total_paise"] == 400_000
    third = decide(engine, mm, 200_000)
    assert third.outcome == "ESCALATE" and {v.rule_id for v in third.violations} == {"MANDATE-LIMIT-001"}
    assert "400000 + 200000 exceeds the limit 500000" in third.violations[0].reason


def test_the_mock_is_a_second_wall_a_third_payment_is_refused_even_if_policy_is_skipped(mm, conn):
    pay(mm, "p1", 200_000), pay(mm, "p2", 200_000)
    r = pay(mm, "p3", 200_000)
    assert r.status == 409 and r.body["error"] == "MANDATE_LIMIT_EXCEEDED"
    assert count(conn, "mandate_debits") == 2
    assert pay(mm, "p4", 100_000).status == 201                       # exactly up to the limit is allowed
    assert pay(mm, "p5", 1).status == 409


def test_watch_out_limit_is_cumulative_not_per_payment(mm):
    """A per-payment check would pass every 1000.00 payment; five reach the limit, the sixth must not pass."""
    for i in range(5):
        assert pay(mm, f"c{i}", 100_000).status == 201
    assert pay(mm, "c5", 100_000).status == 409
    assert mm.get("/mock/mandate", mid=M).body["mandate"]["remaining_paise"] == 0


def test_the_limit_resets_in_a_new_month_on_the_sim_clock(mm, clock):
    assert pay(mm, "a", LIMIT).status == 201 and pay(mm, "b", 1).status == 409
    clock.advance(timedelta(days=40))
    r = pay(mm, "c", 100_000)
    assert r.status == 201 and r.body["month_total_paise"] == 100_000


def test_purpose_other_than_paytm_dues_is_refused_and_escalated(mm, engine):
    r = pay(mm, "x", 100, purpose="loan_emi")
    assert r.status == 409 and r.body["error"] == "PURPOSE_NOT_ALLOWED" and\
         mm.get("/mock/mandate/receipts", mid=M).body["receipts"] == []
    f = facts_from_mock(mm)
    d = engine.evaluate(ActionIntent("collect_mandate_payment", {"merchant_id": M, "amount_paise": 100}, principal=Principal("AGENT", "munim"),
                                     allowlist=("collect_mandate_payment",), facts={**f, "mandate_purpose": "loan_emi"}))
    assert d.outcome == "ESCALATE"


def test_no_mandate_no_payment(app, mock):
    app.include_router(mandate.router)
    r = pay(mock, "n", 100)
    assert r.status == 409 and r.body["error"] == "NO_ACTIVE_MANDATE"
    assert mock.get("/mock/mandate", mid=M).body == {"merchant_id": M, "mandate": None}


def test_a_replay_is_one_debit_with_one_receipt_and_a_new_key_is_a_new_debit(mm, conn):
    a, b = pay(mm, "same", 150_000), pay(mm, "same", 150_000)
    assert a.body == b.body and b.replayed and count(conn, "mandate_debits") == 1
    assert a.body["receipt_id"].startswith("RCPT-") and a.body["simulated"] is True
    c = pay(mm, "other", 150_000)
    assert c.body["receipt_id"] != a.body["receipt_id"]
    assert [r["amount_paise"] for r in mm.get("/mock/mandate/receipts", mid=M).body["receipts"]] == [150_000, 150_000]
    assert pay(mm, "same", 1).status == 422                           # same key, different request


def test_replacing_the_mandate_revokes_the_old_one_and_the_month_still_counts(mm, conn):
    pay(mm, "p", 300_000)
    assert mm.write("POST", "/mock/mandate", key="set-2", json={"merchant_id": M, "monthly_limit_paise": 400_000}).status == 201
    m = mm.get("/mock/mandate", mid=M).body["mandate"]
    assert m["monthly_limit_paise"] == 400_000 and m["month_total_paise"] == 300_000 and m["remaining_paise"] == 100_000
    assert count(conn, "mandates", f"merchant_id='{M}' AND status='ACTIVE'") == 1
    assert pay(mm, "q", 100_001).status == 409


def test_unknown_merchant(mm):
    assert mm.write("POST", "/mock/mandate/payments", key="u", json={"merchant_id": "M-9999", "amount_paise": 1}).body["error"] == "MERCHANT_NOT_FOUND"
