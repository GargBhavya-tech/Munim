"""Service playbooks C1 (settlement), C3 (failed payment), C4 (merchant refund), C5 (bank/KYC detail update).

Holds the scripted deciders (SCRIPTS) shared with tests/test_scenarios_service_a.py, the structural tests of the four playbook
files and one guard test per "Watch out" of tickets #78, #80, #81, #82. The scripted decider stands in for the model: it proposes,
the playbook, the policy engine and the executor decide.
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from core.actions.types import ActionIntent
from core.engine.clock import iso, parse_iso
from core.engine.diagnose import action_permitted
from core.engine.playbook import load_playbook, load_playbooks
from core.engine.scripted import scripted_decider
from core.engine.workflow import Decision
from packs.paytm_merchant.actions import build_registry
from tests.conftest import count
from tests.world import PB_DIR, World

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "packs" / "paytm_merchant" / "kb"
IDS = ("C1_SETTLEMENT_DELAY", "C3_PAYMENT_FAILED", "C4_MERCHANT_REFUND", "C5_DETAIL_UPDATE")
MERCHANT_INITIATED = ("MERCHANT_MESSAGE", "LEAD_REPLY")
FOLLOW_UP_STAND_IN_HOURS = 72     # TEST ONLY: no system read returns a dispute timeline date (see report); real value comes from the system

# ---------------------------------------------------------------------------------------------------------------------
# scripted deciders. Each factory takes the World and returns {(playbook_id, step): entry}.
# ---------------------------------------------------------------------------------------------------------------------
GENERIC_DISPUTE_LINE = ("Your customer's payment is being tracked. Failed payments are reversed automatically within the payment "
                        "network's timeline, and we have raised a dispute for this transaction. We will check again at the follow-up time.")
LINK_LINE = ("I cannot change bank details in chat. Please use the secure link in your Paytm app to update them yourself. "
             "Never share account numbers or OTPs here.")
REFUND_LINE = "I have sent a refund request to your Paytm app. Please confirm it there. It is processed only after you confirm in the app."


def _text(ctx) -> str:
    return str(ctx.event.payload.get("text") or "")


def _mid(ctx) -> str:
    return ctx.case.merchant_id


def _follow_ups(world, ctx) -> list:
    rows = world.reader("get_schedule", {"merchant_id": _mid(ctx)})["follow_ups"]
    return [r for r in rows if r["case_id"] == ctx.case.case_id and r["status"] == "SCHEDULED"]


def c1_scripts(world) -> dict:
    def bank_fix(ctx):
        msg = LINK_LINE if ctx.event.type in MERCHANT_INITIATED else None     # a proactive event gets the link only (one contact per day)
        return Decision(action=("send_bank_update_link", {"merchant_id": _mid(ctx)}), message_en=msg, channel="app")

    def settled(ctx):
        mid = _mid(ctx)
        st = world.reader("get_settlements", {"merchant_id": mid})["settlements"][0]
        text = _text(ctx).lower()
        if "clear" in text and "hold" in text:                                  # a model told to clear the hold; the agent has no such action
            return Decision(action=("clear_risk_hold", {"settlement_id": st["settlement_id"]}))
        if st["status"] != "PENDING":
            return Decision()
        due, now = parse_iso(st["expected_at"]), world.clock.now()
        if now < due:
            if _follow_ups(world, ctx):
                return Decision()
            body = f"Your settlement is expected at {st['expected_at']} according to our system. We will check again then."
            return Decision(action=("schedule_follow_up", {"merchant_id": mid, "due_at": st["expected_at"], "case_id": ctx.case.case_id,
                                                           "note": "settlement follow-up"}),
                            message_en=body, follow_up_hours=(due - now).total_seconds() / 3600, channel="app")
        tickets = world.reader("get_tickets", {"merchant_id": mid})["tickets"]
        if any(t["system_object"] == st["settlement_id"] and t["status"] == "OPEN" for t in tickets):
            return Decision()
        return Decision(action=("raise_ticket", {"merchant_id": mid, "type": "SETTLEMENT", "system_object": st["settlement_id"],
                                                 "summary": "Settlement still pending after the expected time", "case_id": ctx.case.case_id}))
    return {("C1_SETTLEMENT_DELAY", "bank_fix"): bank_fix, ("C1_SETTLEMENT_DELAY", "settled"): settled}


def c3_scripts(world) -> dict:
    def newest_payment(ctx):
        return world.reader("get_transactions", {"merchant_id": _mid(ctx)})["payments"][0]

    def dispute(ctx):
        p = newest_payment(ctx)
        return Decision(action=("raise_ticket", {"merchant_id": _mid(ctx), "type": "DISPUTE", "system_object": p["payment_id"],
                                                 "summary": "Customer debited, payment failed", "case_id": ctx.case.case_id}))

    def reversal(ctx):
        if "refund" in _text(ctx).lower():                                      # injected instruction: a refund is not part of this playbook
            return Decision(action=("create_refund_request", {"merchant_id": _mid(ctx), "payment_id": newest_payment(ctx)["payment_id"], "amount_paise": 200000}))
        if _follow_ups(world, ctx):
            return Decision()
        due = world.clock.now() + timedelta(hours=FOLLOW_UP_STAND_IN_HOURS)
        return Decision(action=("schedule_follow_up", {"merchant_id": _mid(ctx), "due_at": iso(due), "case_id": ctx.case.case_id, "note": "dispute follow-up"}),
                        message_en=GENERIC_DISPUTE_LINE, follow_up_hours=FOLLOW_UP_STAND_IN_HOURS, channel="app")
    return {("C3_PAYMENT_FAILED", "dispute"): dispute, ("C3_PAYMENT_FAILED", "reversal"): reversal}


def c4_scripts(world) -> dict:
    def request(ctx):
        p = world.reader("get_transactions", {"merchant_id": _mid(ctx)})["payments"][0]
        amount = p["amount_paise"]
        m = re.search(r"(\d+)\s*rupaye", _text(ctx))
        if m:
            amount = int(m.group(1)) * 100
        return Decision(action=("create_refund_request", {"merchant_id": _mid(ctx), "payment_id": p["payment_id"], "amount_paise": amount}),
                        message_en=REFUND_LINE, channel="app")
    return {("C4_MERCHANT_REFUND", "request"): request}      # step `confirmation`: nothing to do but wait for the merchant's app


def c5_scripts(world) -> dict:
    def link(ctx):
        msg = LINK_LINE if ctx.event.type in MERCHANT_INITIATED else None
        m = re.search(r"\d{9,}", _text(ctx))
        if m:                                                                   # a bad model that repeats the number: code must block it
            msg = f"Okay, I have changed your settlement account to {m.group(0)}."
        return Decision(action=("send_bank_update_link", {"merchant_id": _mid(ctx)}), message_en=msg, channel="app")

    def verified(ctx):
        kyc = world.reader("get_kyc_status", {"merchant_id": _mid(ctx)})
        if kyc["status"] != "APPROVED":
            return Decision(action=("send_kyc_resume_link", {"merchant_id": _mid(ctx)}), channel="app")
        return Decision()
    return {("C5_DETAIL_UPDATE", "link"): link, ("C5_DETAIL_UPDATE", "verified"): verified}


SCRIPTS = {"C1_SETTLEMENT_DELAY": c1_scripts, "C3_PAYMENT_FAILED": c3_scripts, "C4_MERCHANT_REFUND": c4_scripts, "C5_DETAIL_UPDATE": c5_scripts}


def build_world(db_path, clock, mid="M-0006") -> World:
    """A World whose decider is the scripted table of all four playbooks."""
    world = World(db_path, clock, None, mid=mid)
    table: dict = {}
    for factory in SCRIPTS.values():
        table.update(factory(world))
    world.engine.decider = scripted_decider(table)
    return world


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------
def rows(world, sql, args=()):
    return [dict(r) for r in world.conn.execute(sql, args).fetchall()]


def policy_refusals(world) -> list[tuple[str, str]]:
    """(action, rule_id) of every refused policy decision in the ledger."""
    import json
    out = []
    for r in world.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY' ORDER BY seq"):
        p = json.loads(r["payload_json"])
        if p.get("allow") is False:
            out += [(p.get("action"), rid) for rid in p.get("rule_ids", [])]
    return out


def message_bodies(world) -> list[str]:
    import json
    return [json.loads(r["payload_json"]).get("text", "") for r in world.conn.execute("SELECT payload_json FROM ledger WHERE kind='MESSAGE_SENT'")]


@pytest.fixture
def pbs():
    return load_playbooks(PB_DIR, build_registry())


@pytest.fixture
def world(db_path, clock):
    def make(mid):
        return build_world(db_path, clock, mid)
    return make


def evaluate(pbs, pid, src, **ns):
    return bool(pbs[pid].expr(src).evaluate(ns))


# ---------------------------------------------------------------------------------------------------------------------
# structure: the four files load and carry the powers the tickets allow, no more
# ---------------------------------------------------------------------------------------------------------------------
def test_the_four_playbooks_load_with_the_exact_ids_and_the_intent_map_routes_to_them(pbs):
    from core.engine.intents import INTENT_TO_PLAYBOOK
    assert set(IDS) <= set(pbs)
    for intent, pid in (("SETTLEMENT_MISSING", "C1_SETTLEMENT_DELAY"), ("CUSTOMER_PAYMENT_FAILED", "C3_PAYMENT_FAILED"),
                        ("REFUND_REQUEST", "C4_MERCHANT_REFUND"), ("CHANGE_BANK", "C5_DETAIL_UPDATE")):
        assert INTENT_TO_PLAYBOOK[intent] == pid
        assert intent in pbs[pid].triggers.intents
    for pid in IDS:
        assert pbs[pid].half == "service" and pbs[pid].priority_lane == "P1"


def test_no_playbook_can_name_an_action_that_changes_details_clears_a_hold_or_confirms_a_refund(pbs):
    banned = {"clear_risk_hold", "change_bank_details", "change_settlement_account", "approve_kyc", "accept_documents_in_chat",
              "request_otp", "apply_fee_waiver", "create_device_replacement", "create_closure_request", "retrigger_bank_verification",
              "send_partner_application_link"}
    for pid in IDS:
        assert not banned & {a.name for a in pbs[pid].allowed_actions}, pid


def test_each_playbook_allows_exactly_the_actions_of_its_catalogue_row(pbs):
    allowed = {pid: {a.name for a in pbs[pid].allowed_actions} for pid in IDS}
    assert allowed["C1_SETTLEMENT_DELAY"] == {"send_bank_update_link", "schedule_follow_up", "raise_ticket", "send_message"}
    assert allowed["C3_PAYMENT_FAILED"] == {"raise_ticket", "schedule_follow_up", "send_message"}
    assert allowed["C4_MERCHANT_REFUND"] == {"create_refund_request", "schedule_follow_up", "send_message"}
    assert allowed["C5_DETAIL_UPDATE"] == {"send_bank_update_link", "send_kyc_resume_link", "schedule_follow_up", "send_message"}
    assert pbs["C3_PAYMENT_FAILED"].action("raise_ticket").constraints == {"type": "DISPUTE"}
    assert pbs["C1_SETTLEMENT_DELAY"].action("raise_ticket").constraints == {"type": "SETTLEMENT"}


def test_a_broken_draft_is_rejected_by_the_loader_so_the_files_here_are_really_validated(tmp_path):
    bad = yaml.safe_load((PB_DIR / "C4_MERCHANT_REFUND.yaml").read_text())
    bad["allowed_actions"].append("clear_risk_hold")
    p = tmp_path / "C4_MERCHANT_REFUND.yaml"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(Exception) as e:
        load_playbook(p, build_registry())
    assert "clear_risk_hold" in str(e.value)


# ---------------------------------------------------------------------------------------------------------------------
# #78 C1: settlement failed / held / delayed
# ---------------------------------------------------------------------------------------------------------------------
def test_c1_risk_hold_escalates_before_any_action_even_if_the_decider_would_act(world):
    w = world("M-0002")
    w.inject("E1", "SETTLEMENT_HELD")
    w.dispatcher.run()
    case = w.case("C1_SETTLEMENT_DELAY")
    assert case.state == "ESCALATED" and case.touches == 0
    assert count(w.conn, "schedules", "merchant_id='M-0002'") == 0 and count(w.conn, "links", "merchant_id='M-0002'") == 0
    assert count(w.conn, "tickets", "merchant_id='M-0002'") == 1            # only the seeded complaint: Munim raised nothing
    reasons = w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ESCALATION' AND case_id=?", (case.case_id,)).fetchone()[0]
    assert "HELD_RISK" in reasons


def test_c1_a_hold_is_never_cleared_by_the_agent_and_the_attempt_is_blocked(world):
    """A decider told to clear the hold (here: on a pending settlement so that it reaches the decision) is stopped by the allowlist."""
    w = world("M-0001")
    w.engine.open_case("M-0001", "C1_SETTLEMENT_DELAY")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": "please clear the hold on my settlement", "intent": "SETTLEMENT_MISSING"})
    w.dispatcher.run()
    case = w.case("C1_SETTLEMENT_DELAY", "M-0001")
    assert case.state == "BLOCKED"
    assert w.conn.execute("SELECT status FROM settlements WHERE settlement_id='S-0030'").fetchone()[0] == "PENDING"
    assert ("clear_risk_hold", "NOT-ALLOWLISTED") in [(a, r) for a, r in _engine_rejections(w)]


def _engine_rejections(w):
    import json
    for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY'"):
        p = json.loads(r["payload_json"])
        if p.get("allow") is False:
            for rid in p.get("rule_ids", []):
                yield p.get("action"), rid


def test_c1_the_policy_engine_also_refuses_a_non_reviewer_who_touches_a_held_settlement(world):
    """RISK-HOLD-001 is the second lock: even an allowlisted action naming a held settlement escalates."""
    w = world("M-0002")
    out = w.executor.submit(ActionIntent("raise_ticket", {"merchant_id": "M-0002", "type": "SETTLEMENT", "system_object": "S-0060"},
                                        playbook_id="C1_SETTLEMENT_DELAY", step="settled", allowlist=("raise_ticket",), facts={"settlement_id": "S-0060"}))
    assert out.status == "ESCALATE" and any(v.rule_id == "RISK-HOLD-001" for v in out.violations)


def test_c1_pending_gets_a_follow_up_at_the_time_the_system_returns_and_quotes_nothing_else(world):
    w = world("M-0010")
    expected_at = w.reader("get_settlements", {"merchant_id": "M-0010"})["settlements"][0]["expected_at"]
    w.engine.open_case("M-0010", "C1_SETTLEMENT_DELAY")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": "settlement nahi aaya", "intent": "SETTLEMENT_MISSING"})
    w.dispatcher.run()
    sched = rows(w, "SELECT due_at, status FROM schedules WHERE merchant_id='M-0010'")
    assert sched == [{"due_at": expected_at, "status": "SCHEDULED"}] and expected_at == "2026-10-31T10:00:00+05:30"
    bodies = message_bodies(w)
    assert len(bodies) == 1 and expected_at in bodies[0]
    assert not re.search(r"\b(T\+\d|tomorrow|by evening|within \d+)", bodies[0], re.I)
    timer = w.conn.execute("SELECT due_at FROM timers WHERE status='PENDING'").fetchone()
    assert timer and parse_iso(timer[0]) == parse_iso(expected_at)


def test_c1_failed_bank_sends_the_secure_link_and_never_takes_an_account_number(world):
    w = world("M-0001")
    w.advance_settlement("S-0030", "adv", to="FAILED_BANK")
    w.dispatcher.run()
    assert count(w.conn, "links", "merchant_id='M-0001' AND kind='BANK_UPDATE'") == 1
    # a reply that carries an account number or IFSC is refused by KYC-NO-CHAT-001, whichever playbook sends it
    for body in ("Please send your new account number 123456789012 here", "IFSC HDFC0001234 is noted"):
        out = w.executor.submit(ActionIntent("send_message", {"merchant_id": "M-0001", "channel": "app", "body": body, "language": "en"}, proactive=False))
        assert out.status == "REFUSED" and any(v.rule_id == "KYC-NO-CHAT-001" for v in out.violations), body


def test_c1_the_money_limit_boundary_is_inclusive_and_a_regulator_style_complaint_escalates(pbs):
    src = "settlement.amount_paise >= 1000000"
    assert src in pbs["C1_SETTLEMENT_DELAY"].escalate_when
    assert evaluate(pbs, "C1_SETTLEMENT_DELAY", src, **{"settlement.amount_paise": 1000000})
    assert not evaluate(pbs, "C1_SETTLEMENT_DELAY", src, **{"settlement.amount_paise": 999999})
    limit = yaml.safe_load((ROOT / "packs/paytm_merchant/policy/limits.yaml").read_text())["money_limit"]["threshold_paise"]
    assert limit == 1000000                                                     # the playbook literal must equal limits.yaml
    complaint = "intent in ['COMPLAINT', 'WANTS_HUMAN']"
    assert evaluate(pbs, "C1_SETTLEMENT_DELAY", complaint, intent="COMPLAINT") and not evaluate(pbs, "C1_SETTLEMENT_DELAY", complaint, intent="SETTLEMENT_MISSING")


def test_c1_follow_up_precondition_blocks_a_follow_up_on_a_failed_bank_settlement(world, pbs):
    w = world("M-0001")
    w.advance_settlement("S-0030", "adv", to="FAILED_BANK")
    ok, why = action_permitted(pbs["C1_SETTLEMENT_DELAY"], "schedule_follow_up", w.reader, "M-0001", {}, prior_count=0)
    assert not ok and why.startswith("PRECONDITION")
    ok, why = action_permitted(pbs["C1_SETTLEMENT_DELAY"], "send_bank_update_link", w.reader, "M-0001", {}, prior_count=1)
    assert not ok and why.startswith("MAX_PER_CASE")


# ---------------------------------------------------------------------------------------------------------------------
# #80 C3: customer debited, payment failed
# ---------------------------------------------------------------------------------------------------------------------
def test_c3_dispute_ticket_carries_the_transaction_and_a_follow_up_is_scheduled(world):
    w = world("M-0010")
    w.inject("E1", "PAYMENT_FAILED")
    w.dispatcher.run()
    t = rows(w, "SELECT type, status, system_object, case_id FROM tickets WHERE merchant_id='M-0010'")
    assert t == [{"type": "DISPUTE", "status": "OPEN", "system_object": "P-61172", "case_id": w.case("C3_PAYMENT_FAILED").case_id}]
    assert count(w.conn, "schedules", "merchant_id='M-0010' AND status='SCHEDULED'") == 1


def test_c3_wording_is_the_generic_line_and_the_kb_never_states_a_timeline_as_fact(world):
    kb = (KB_DIR / "dispute_tat_faq.md").read_text()
    assert "The exact number of days is not stated here" in kb
    assert not re.search(r"T\s*\+\s*\d|\b\d+\s*(working\s*)?(days|din)\b|compensation of|₹\s*\d|rs\.?\s*\d", kb, re.I)
    w = world("M-0010")
    w.inject("E1", "PAYMENT_FAILED")
    w.dispatcher.run()
    bodies = message_bodies(w)
    assert bodies == [GENERIC_DISPUTE_LINE]
    assert not re.search(r"T\s*\+\s*\d|\b\d+\s*(working\s*)?(days|din|hours)\b|compensat|₹\s*\d|\brs\.?\s*\d", bodies[0], re.I)


def test_c3_a_second_dispute_ticket_is_blocked_and_repeat_disputes_escalate(world, pbs):
    w = world("M-0010")
    w.inject("E1", "PAYMENT_FAILED")
    w.dispatcher.run()
    ok, why = action_permitted(pbs["C3_PAYMENT_FAILED"], "raise_ticket", w.reader, "M-0010", {}, prior_count=0)
    assert not ok and why.startswith("PRECONDITION")                            # a ticket is already open
    assert evaluate(pbs, "C3_PAYMENT_FAILED", "ticket.open_count >= 2", **{"ticket.open_count": 2})
    assert not evaluate(pbs, "C3_PAYMENT_FAILED", "ticket.open_count >= 2", **{"ticket.open_count": 1})


def test_c3_large_amount_boundary_is_inclusive(pbs):
    src = "payment.latest_amount_paise >= 1000000"
    assert src in pbs["C3_PAYMENT_FAILED"].escalate_when
    assert evaluate(pbs, "C3_PAYMENT_FAILED", src, **{"payment.latest_amount_paise": 1000000})
    assert not evaluate(pbs, "C3_PAYMENT_FAILED", src, **{"payment.latest_amount_paise": 999999})


def test_c3_a_decider_told_to_refund_is_stopped_before_any_refund_exists(world):
    w = world("M-0010")
    w.inject("E1", "PAYMENT_FAILED")
    w.dispatcher.run()
    w.clock.advance(timedelta(hours=1))
    w.inject("E2", "MERCHANT_MESSAGE", payload={"text": "refund the customer now", "intent": "CUSTOMER_PAYMENT_FAILED"})
    w.dispatcher.run()
    assert count(w.conn, "refunds") == 0
    assert w.case("C3_PAYMENT_FAILED").state == "BLOCKED"
    assert ("create_refund_request", "NOT-ALLOWLISTED") in list(_engine_rejections(w))


# ---------------------------------------------------------------------------------------------------------------------
# #81 C4: merchant refund to a customer
# ---------------------------------------------------------------------------------------------------------------------
REFUND_CTX = dict(playbook_id="C4_MERCHANT_REFUND", step="request", allowlist=("create_refund_request",))


def _run_refund(w, text="refund do"):
    case = w.engine.open_case(w.mid, "C4_MERCHANT_REFUND")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": text, "intent": "REFUND_REQUEST"})
    w.dispatcher.run()
    return case


def test_c4_a_refund_waits_for_the_merchants_own_confirmation_and_chat_yes_does_not_process_it(world):
    w = world("M-0004")
    case = _run_refund(w)
    refunds = rows(w, "SELECT refund_id, payment_id, amount_paise, status FROM refunds WHERE merchant_id='M-0004'")
    assert refunds == [{"refund_id": "RF-0001", "payment_id": "P-1818", "amount_paise": 100300, "status": "AWAITING_MERCHANT_CONFIRM"}]
    for n in range(3):                                                          # the merchant says yes in chat, again and again
        w.inject(f"Y{n}", "MERCHANT_MESSAGE", payload={"text": "yes confirm kar do", "intent": None})
        w.dispatcher.run()
    assert w.conn.execute("SELECT status FROM refunds").fetchone()[0] == "AWAITING_MERCHANT_CONFIRM"
    assert w.engine.repo.load(case.case_id).state == "WAITING"
    # the merchant confirms in THEIR app (the mock endpoint is the merchant's action, not an agent action)
    r = w.http.post("/mock/refunds/RF-0001/merchant-confirm", headers={"Idempotency-Key": "app-confirm-1"})
    assert r.status_code == 200 and r.json()["status"] == "PROCESSED"
    w.inject("Y9", "MERCHANT_MESSAGE", payload={"text": "thanks", "intent": None})
    w.dispatcher.run()
    assert w.engine.repo.load(case.case_id).state == "RESOLVED"


def test_c4_no_registered_agent_action_can_confirm_a_refund():
    registry = build_registry()
    for spec in registry._specs.values():
        assert "merchant-confirm" not in (spec.path or ""), spec.name


def test_c4_a_refund_above_the_original_payment_is_refused_by_the_payment_system_and_nothing_is_created(world):
    w = world("M-0004")
    _run_refund(w, "refund karna hai, 5000 rupaye wapas karne hain")           # Rs 5,000 on a Rs 1,003 payment
    assert count(w.conn, "refunds") == 0
    res = [r for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ACTION_RESULT'") if "REFUND_EXCEEDS_PAYMENT" in r[0]]
    assert len(res) == 1
    assert w.case("C4_MERCHANT_REFUND").state == "WAITING"


def test_c4_money_limit_policy_escalates_an_over_limit_refund_even_without_the_playbook_rule(world):
    """Second lock: MONEY-LIMIT-001 sits in the policy engine, so it holds even if a playbook forgot its own escalation rule."""
    w = world("M-0004")
    out = w.executor.submit(ActionIntent("create_refund_request", {"merchant_id": "M-0004", "payment_id": "P-1818", "amount_paise": 1000000}, **REFUND_CTX))
    assert out.status == "ESCALATE" and any(v.rule_id == "MONEY-LIMIT-001" for v in out.violations)
    assert count(w.conn, "refunds") == 0
    just_under = w.executor.submit(ActionIntent("create_refund_request", {"merchant_id": "M-0004", "payment_id": "P-1818", "amount_paise": 999999}, **REFUND_CTX))
    assert not any(v.rule_id == "MONEY-LIMIT-001" for v in just_under.violations)   # boundary: 999,999 passes policy (the mock then refuses > payment)


def test_c4_only_one_open_refund_request_per_case(world, pbs):
    w = world("M-0004")
    _run_refund(w)
    pb = pbs["C4_MERCHANT_REFUND"]
    ok, why = action_permitted(pb, "create_refund_request", w.reader, "M-0004", {}, prior_count=0)
    assert not ok and why.startswith("PRECONDITION")                            # a request is already awaiting the merchant
    ok, why = action_permitted(pb, "create_refund_request", w.reader, "M-0004", {}, prior_count=1)
    assert not ok and why.startswith("MAX_PER_CASE")


# ---------------------------------------------------------------------------------------------------------------------
# #82 C5: bank / KYC detail update
# ---------------------------------------------------------------------------------------------------------------------
def test_c5_account_number_in_chat_gets_the_link_a_blocked_reply_and_a_ledgered_KYC_NO_CHAT_001(world):
    w = world("M-0004")
    before = rows(w, "SELECT account_last4, ifsc, status FROM bank WHERE merchant_id='M-0004'")
    case = w.engine.open_case("M-0004", "C5_DETAIL_UPDATE")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": "Change my bank account to 123456789012", "intent": "CHANGE_BANK"})
    w.dispatcher.run()
    assert count(w.conn, "links", "merchant_id='M-0004' AND kind='BANK_UPDATE'") == 1       # the link went out
    assert ("send_message", "KYC-NO-CHAT-001") in policy_refusals(w)                       # the reply repeating the number was refused and ledgered
    assert w.engine.repo.load(case.case_id).state == "BLOCKED"
    assert rows(w, "SELECT account_last4, ifsc, status FROM bank WHERE merchant_id='M-0004'") == before   # Munim changed nothing
    assert not any("123456789012" in b for b in message_bodies(w))


def test_c5_the_refusal_text_is_sent_without_any_digits_when_the_decider_behaves(world):
    w = world("M-0004")
    w.engine.open_case("M-0004", "C5_DETAIL_UPDATE")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": "I want to change my bank account", "intent": "CHANGE_BANK"})
    w.dispatcher.run()
    assert message_bodies(w) == [LINK_LINE] and not re.search(r"\d", LINK_LINE)
    assert not [p for p in policy_refusals(w)]
    assert w.case("C5_DETAIL_UPDATE").state == "RESOLVED"


def test_c5_pressure_language_escalates_before_any_link(world):
    w = world("M-0004")
    w.engine.open_case("M-0004", "C5_DETAIL_UPDATE")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": "urgent! speak to a person now and change my bank account", "intent": "WANTS_HUMAN"})
    w.dispatcher.run()
    assert w.case("C5_DETAIL_UPDATE").state == "ESCALATED"
    assert count(w.conn, "links") == 0


def test_c5_a_healthy_merchant_still_gets_the_link_because_the_step_is_done_only_when_a_link_went_out(world, pbs):
    pb = pbs["C5_DETAIL_UPDATE"]
    assert pb.steps[0].done == "touches >= 1"
    w = world("M-0004")
    _ = w.engine.open_case("M-0004", "C5_DETAIL_UPDATE")
    w.inject("E1", "MERCHANT_MESSAGE", payload={"text": "bank badalna hai", "intent": "CHANGE_BANK"})
    w.dispatcher.run()
    assert count(w.conn, "links", "merchant_id='M-0004' AND kind='BANK_UPDATE'") == 1


def test_c5_kyc_link_only_when_kyc_is_not_approved(world, pbs):
    w = world("M-0004")
    ok, why = action_permitted(pbs["C5_DETAIL_UPDATE"], "send_kyc_resume_link", w.reader, "M-0004", {}, prior_count=0)
    assert not ok and why.startswith("PRECONDITION")
    w2 = world("M-0001")
    assert action_permitted(pbs["C5_DETAIL_UPDATE"], "send_kyc_resume_link", w2.reader, "M-0001", {}, prior_count=0) == (True, None)


# ---------------------------------------------------------------------------------------------------------------------
# the scripted decider table is itself checked: every playbook step that can act has an entry
# ---------------------------------------------------------------------------------------------------------------------
def test_scripts_cover_the_acting_steps_of_every_playbook(world):
    w = world("M-0006")
    keys = set()
    for factory in SCRIPTS.values():
        keys |= set(factory(w))
    assert {("C1_SETTLEMENT_DELAY", "bank_fix"), ("C1_SETTLEMENT_DELAY", "settled"), ("C3_PAYMENT_FAILED", "dispute"), ("C3_PAYMENT_FAILED", "reversal"),
            ("C4_MERCHANT_REFUND", "request"), ("C5_DETAIL_UPDATE", "link"), ("C5_DETAIL_UPDATE", "verified")} <= keys
