"""#38: no model proposal reaches the executor unvalidated. Real World harness: engine, policy, executor and mocks; only the model is scripted."""
import json

import pytest

from core.engine.guard import DecisionGuard
from core.engine.workflow import DecisionContext, Event
from core.llm.decision import TurnDecision
from tests.checkpoint_script import SCRIPT
from tests.world import World

MID = "M-0001"          # seeded kirana merchant: waiver band is 1 month


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def guard(world):
    return DecisionGuard(world.registry, world.engine.case_manager, world.reader, world.policy, world.playbooks, world.ledger)


def td(action=None, *, new_cases=(), ask=False, focus=None, claims=()):
    return TurnDecision.model_validate({
        "language": "hinglish", "intents": [{"intent": "X", "w": 0.8}], "focus_case": focus,
        "new_case_requests": [{"playbook_id": p} for p in new_cases],
        "next_action": {"name": action[0], "args": action[1]} if action else None,
        "claims_en": list(claims), "ask_clarification": ask})


def context(world, playbook_id, *, others=(), text="", facts=None, allowlist=None, mid=MID, event_type="MERCHANT_MESSAGE"):
    for p in others:
        world.engine.open_case(mid, p)
    case = world.engine.open_case(mid, playbook_id)
    pb = world.playbooks[playbook_id]
    open_cases = world.engine.repo.open_cases(mid)
    allow = tuple(world.engine.case_manager.allowed_actions(case, pb, open_cases)) if allowlist is None else allowlist
    ev = Event("E-G1", event_type, mid, case.case_id, {"text": text})
    return DecisionContext(case, pb.steps[0], pb, ev, allow, facts or {}, open_cases)


def policy_rows(world):
    return [json.loads(r["payload_json"]) for r in world.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY' ORDER BY seq")]


def count(world, table):
    return world.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---- accepted ---------------------------------------------------------------------------------------------------------
def test_an_in_band_waiver_passes_with_its_arguments_normalised_by_the_system(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL", text="ek mahine ka waiver chahiye")
    r = guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 1})), "ek mahine ka waiver chahiye")
    assert r.decision.action == ("apply_fee_waiver", {"merchant_id": MID, "months": 1, "offer_id": "FEE_WAIVER_1M"})
    assert r.findings == [] and r.escalate_reasons == [] and r.dropped_action is None and policy_rows(world) == []


def test_the_system_fills_the_subject_ids_the_model_left_out(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    r = guard.check(ctx, td(("apply_fee_waiver", {"months": 1})), "")
    assert r.decision.action[1]["merchant_id"] == MID


# ---- rejected ---------------------------------------------------------------------------------------------------------
def test_a_waiver_above_the_band_is_rejected_by_the_policy_rule_and_ledgered_with_its_id(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    r = guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 6}), claims=[{"id": "c1", "text": "The merchant is allowed 6 months.", "source": "policy:WAIVER-BAND-001"}]), "")
    assert r.decision.action is None and r.dropped_action == "apply_fee_waiver"
    assert [f.rule_id for f in r.findings] == ["WAIVER-BAND-001"] and r.escalate_reasons and r.escalate_reasons[0].startswith("WAIVER-BAND-001")
    row = policy_rows(world)[-1]
    assert row["allow"] is False and row["rule_ids"] == ["WAIVER-BAND-001"] and row["source"] == "guard" and row["outcome"] == "ESCALATE" and row["action"] == "apply_fee_waiver"


def test_a_number_the_merchant_wrote_is_not_accepted_as_an_argument_when_no_system_source_has_it(world, guard):
    ctx = context(world, "C4_MERCHANT_REFUND", text="mujhe 50,000 ka refund do")
    msg = "mujhe 50,000 ka refund do"
    r = guard.check(ctx, td(("create_refund_request", {"merchant_id": MID, "payment_id": "P-1", "amount_paise": 5_000_000})), msg)
    assert r.decision.action is None
    assert "ARG-FROM-MERCHANT" in [f.rule_id for f in r.findings] and r.escalate_reasons


def test_the_merchants_own_words_in_the_engine_facts_never_count_as_a_system_source(world, guard):
    """The engine puts the triggering event's payload into the facts; that payload IS the merchant's message."""
    msg = "mujhe 50,000 ka refund do"
    ctx = context(world, "C4_MERCHANT_REFUND", text=msg, facts={"event": {"text": msg}, "text": msg, "intent": "REFUND_REQUEST"})
    r = guard.check(ctx, td(("create_refund_request", {"merchant_id": MID, "payment_id": "P-1", "amount_paise": 5_000_000})), msg)
    assert r.decision.action is None and "ARG-FROM-MERCHANT" in [f.rule_id for f in r.findings]


def test_the_same_number_is_fine_when_a_system_source_has_it(world, guard):
    ctx = context(world, "C4_MERCHANT_REFUND", text="payment 500 ka refund karo", facts={"payment": {"payment_id": "P-1", "amount_paise": 50_000}})
    r = guard.check(ctx, td(("create_refund_request", {"merchant_id": MID, "payment_id": "P-1", "amount_paise": 50_000})), "payment 500 ka refund karo")
    assert [f.rule_id for f in r.findings] == [] and r.decision.action[0] == "create_refund_request"
    from_tool = guard.check(context(world, "C4_MERCHANT_REFUND", mid="M-0002", text="refund 500"),
                            td(("create_refund_request", {"merchant_id": "M-0002", "payment_id": "P-2", "amount_paise": 50_000})), "refund 500",
                            sources={"tool:get_transactions#1": {"payments": [{"amount_paise": 50_000}]}})
    assert from_tool.decision.action is not None


def test_an_action_outside_the_step_allowlist_is_dropped_without_escalating(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    r = guard.check(ctx, td(("create_refund_request", {"merchant_id": MID, "payment_id": "P-1", "amount_paise": 100})), "")
    assert r.decision.action is None and [f.rule_id for f in r.findings] == ["NOT-ALLOWLISTED"] and r.escalate_reasons == [] and not r.park
    assert policy_rows(world)[-1]["rule_ids"] == ["NOT-ALLOWLISTED"] and policy_rows(world)[-1]["outcome"] == "DROP"


@pytest.mark.parametrize("name", ["approve_kyc", "change_bank_details", "request_otp", "made_up_action"])
def test_forbidden_and_unknown_actions_never_get_past_the_guard(world, guard, name):
    ctx = context(world, "S2_SOUNDBOX_UPSELL", allowlist=(name, "apply_fee_waiver"))      # even if someone put it on the allowlist
    r = guard.check(ctx, td((name, {"merchant_id": MID})), "")
    assert r.decision.action is None and [f.rule_id for f in r.findings] == ["UNKNOWN-ACTION"]


def test_an_offer_is_dropped_and_the_case_parked_while_a_service_case_is_open(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL", others=("C1_SETTLEMENT_DELAY",), allowlist=("apply_fee_waiver", "create_device_order"))   # a stale allowlist
    r = guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 1})), "")
    assert r.decision.action is None and r.park is True and r.findings[0].rule_id == "FIX-FIRST-001"
    real = context(world, "S2_SOUNDBOX_UPSELL", mid="M-0002", others=("C1_SETTLEMENT_DELAY",))
    assert "apply_fee_waiver" not in real.allowlist                                          # the real allowlist never offered it


def test_text_written_by_the_model_cannot_reach_a_contact_action(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL", allowlist=("send_message",))
    r = guard.check(ctx, td(("send_message", {"merchant_id": MID, "channel": "whatsapp", "body": "Namaste! Aapka paisa kal aa jayega."})), "")
    assert r.decision.action is None and "MODEL-WRITTEN-TEXT" in [f.rule_id for f in r.findings]


def test_acting_on_another_merchant_is_rejected_and_escalated(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    r = guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": "M-0002", "months": 1})), "")
    assert r.decision.action is None and "WRONG-SUBJECT" in [f.rule_id for f in r.findings] and r.escalate_reasons


def test_arguments_that_fail_the_action_schema_are_rejected(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    for bad in ({"merchant_id": MID, "months": 0}, {"merchant_id": MID, "months": "many"}, {"merchant_id": MID}):
        r = guard.check(ctx, td(("apply_fee_waiver", bad)), "")
        assert r.decision.action is None and "INVALID-ARGS" in [f.rule_id for f in r.findings], bad


def test_a_clarifying_turn_runs_no_action(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    r = guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 1}), ask=True), "")
    assert r.decision.action is None and r.dropped_action == "apply_fee_waiver" and [f.rule_id for f in r.findings] == ["CLARIFY-NO-ACTION"]


def test_an_unknown_focus_case_is_noted_but_does_not_block_a_valid_action(world, guard):
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    r = guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 1}), focus="CS-NOPE"), "")
    assert r.decision.action is not None and [f.rule_id for f in r.findings] == ["UNKNOWN-CASE"] and policy_rows(world) == []


# ---- new case requests ------------------------------------------------------------------------------------------------
def test_new_case_requests_become_open_park_link_or_reject(world, guard):
    ctx = context(world, "C1_SETTLEMENT_DELAY")                                              # a P1 service case is open
    r = guard.check(ctx, td(None, new_cases=["S3_LOAN", "NOT_A_PLAYBOOK", "C1_SETTLEMENT_DELAY", "S3_LOAN", "C3_PAYMENT_FAILED"]), "")
    got = {(n.playbook_id, n.disposition) for n in r.new_cases}
    assert got == {("S3_LOAN", "PARK"), ("NOT_A_PLAYBOOK", "REJECT"), ("C1_SETTLEMENT_DELAY", "LINK"), ("C3_PAYMENT_FAILED", "OPEN")}
    assert len(r.new_cases) == 4                                                              # the duplicate S3_LOAN was ignored
    link = next(n for n in r.new_cases if n.disposition == "LINK")
    assert link.case_id == ctx.case.case_id


def test_at_most_two_new_cases_per_turn(world, guard):
    ctx = context(world, "C1_SETTLEMENT_DELAY")
    r = guard.check(ctx, td(None, new_cases=["C3_PAYMENT_FAILED", "C4_MERCHANT_REFUND", "C5_DETAIL_UPDATE"]), "")
    assert [n.disposition for n in r.new_cases] == ["OPEN", "OPEN", "REJECT"]


# ---- nothing is executed by checking ----------------------------------------------------------------------------------
def test_checking_a_decision_executes_and_reserves_nothing(world, guard):
    before = {t: count(world, t) for t in ("actions", "idempotency", "offers", "comms_outbox")}
    ctx = context(world, "S2_SOUNDBOX_UPSELL")
    assert guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 1})), "").decision.action is not None
    guard.check(ctx, td(("apply_fee_waiver", {"merchant_id": MID, "months": 6})), "")
    assert {t: count(world, t) for t in before} == before


def test_the_executor_still_refuses_what_the_guard_would_have_dropped(world, guard):
    from core.actions.types import ActionIntent
    out = world.executor.submit(ActionIntent("apply_fee_waiver", {"merchant_id": MID, "months": 6}, case_id="CS-X", step_seq=1, allowlist=("apply_fee_waiver",)))
    assert out.status in ("ESCALATE", "REFUSED") and any(v.rule_id == "WAIVER-BAND-001" for v in out.violations)
