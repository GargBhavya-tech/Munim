"""#71-#74: S2 Soundbox upsell, S3 loan, S4 plan change, S5 win-back. One test per 'Watch out', each of which fails when the mistake is present.

The second half of the file proves the riskiest guards can fail: it plants the mistake (a flag off, a band hard-coded, a precondition
removed, a rule disabled) and shows that a scenario which passes on the real code is reported as a failure on the mutated code."""
import dataclasses
import json
import re
from datetime import timedelta
from itertools import count

import pytest

from core.actions.compensation import SYSTEM as UNDO
from core.eval.scenario import load_scenario
from core.engine.intents import top_intent
from tests.scenario_runner import check, run_scenario
from tests.test_scenarios_sales import KFS_LINE, SC_DIR, act, sales_world, send
from tests.world import PB_DIR, World

_ids = count(1)
OFFERS = {"create_device_order", "apply_fee_waiver", "create_plan_change", "send_partner_application_link"}


# ----------------------------------------------------------------------------------------------------------- helpers
def say(w, pb, text, lang="hinglish"):
    """The merchant writes. Opens the sales case if there is no live one, like the scenario runner does."""
    row = w.conn.execute("SELECT case_id, state FROM cases WHERE merchant_id=? AND playbook_id=? ORDER BY opened_at DESC, case_id DESC", (w.mid, pb)).fetchone()
    if row is None or row["state"] in ("RESOLVED", "DECLINED"):
        w.engine.open_case(w.mid, pb, language=lang)
    w.inject(f"G-{next(_ids)}", "MERCHANT_MESSAGE", payload={"text": text, "intent": top_intent(text, lang)})
    w.dispatcher.run()


def state(w, pb):
    return w.conn.execute("SELECT state FROM cases WHERE merchant_id=? AND playbook_id=? ORDER BY opened_at DESC, case_id DESC", (w.mid, pb)).fetchone()[0]


def n(w, table, where="1=1", args=()):
    return w.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def action_id(w, name):
    for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ACTION_RESULT' ORDER BY seq"):
        p = json.loads(r[0])
        if p.get("action") == name and p.get("status") == "OK":
            return p["action_id"]
    raise AssertionError(f"{name} never ran")


def policy_refusals(w):
    return [json.loads(r[0]) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY' ORDER BY seq") if json.loads(r[0]).get("allow") is False]


def rules_of(w):
    return {rid for p in policy_refusals(w) for rid in p.get("rule_ids", [])}


def world_for(db_path, clock, pb, mid, script=None):
    return sales_world(db_path, clock, pb, mid, script_override=script)


def install_device(w, mid, status="ONLINE"):
    w.conn.execute("INSERT INTO devices(device_id, merchant_id, model, status, battery_pct, signal_pct, last_heartbeat, last_test_announcement_ok, installed_at) "
                   "VALUES (?,?,?,?,90,90,?,1,?)", (f"D-G-{mid}", mid, "Soundbox 4G", status, w.clock.now_iso(), w.clock.now_iso()))


# ========================================================================================================== all four
def test_all_four_are_p3_sales_playbooks_with_the_exact_ids():
    from core.engine.playbook import load_playbooks
    from packs.paytm_merchant.actions import build_registry
    pbs = load_playbooks(PB_DIR, build_registry())
    for pid in ("S2_SOUNDBOX_UPSELL", "S3_LOAN", "S4_PLAN_CHANGE", "S5_WINBACK"):
        assert pbs[pid].priority_lane == "P3" and pbs[pid].half == "sales", pid


def test_every_offer_action_a_sales_playbook_names_is_flagged_offer_in_the_registry(db_path, clock):
    w = World(db_path, clock, {})
    assert OFFERS <= w.registry.offer_actions()
    named = {a.name for pid in ("S2_SOUNDBOX_UPSELL", "S3_LOAN", "S4_PLAN_CHANGE", "S5_WINBACK") for a in w.playbooks[pid].allowed_actions}
    assert OFFERS & named == OFFERS                                           # between them the four playbooks use all four offer actions
    for pid, banned in (("S3_LOAN", {"apply_fee_waiver", "create_device_order", "create_plan_change"}), ("S5_WINBACK", OFFERS)):
        assert not ({a.name for a in w.playbooks[pid].allowed_actions} & banned), pid


@pytest.mark.parametrize("pb,mid,offer", [("S2_SOUNDBOX_UPSELL", "M-0007", "create_device_order"), ("S3_LOAN", "M-0003", "send_partner_application_link"),
                                          ("S4_PLAN_CHANGE", "M-0009", "create_plan_change")])
def test_fix_first_removes_the_offer_from_the_allowlist_while_a_service_case_is_open(db_path, clock, pb, mid, offer):
    w = World(db_path, clock, {}, mid=mid)
    sales = w.engine.open_case(mid, pb)
    assert offer in w.engine.case_manager.allowed_actions(sales, w.playbooks[pb], w.engine.repo.open_cases(mid))
    w.engine.open_case(mid, "C2_SOUNDBOX_FAULT")
    assert offer not in w.engine.case_manager.allowed_actions(sales, w.playbooks[pb], w.engine.repo.open_cases(mid))


# ========================================================================================================= S2 (#71)
def test_s2_order_placed_is_not_done_until_the_device_is_online(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    say(w, "S2_SOUNDBOX_UPSELL", "soundbox lena hai, order kar do")
    assert n(w, "device_orders", "merchant_id='M-0007' AND status='PLACED'") == 1 and state(w, "S2_SOUNDBOX_UPSELL") == "WAITING"
    install_device(w, "M-0007", "OFFLINE")                                    # the courier installed it, but it is not online yet
    w.inject("G-DELIV1", "DEVICE_DELIVERED", payload={})
    w.dispatcher.run()
    assert state(w, "S2_SOUNDBOX_UPSELL") == "WAITING"                        # order PLACED alone, or a device that is offline, closes nothing
    w.conn.execute("UPDATE devices SET status='ONLINE' WHERE merchant_id='M-0007'")
    w.inject("G-DELIV2", "DEVICE_DELIVERED", payload={})
    w.dispatcher.run()
    assert state(w, "S2_SOUNDBOX_UPSELL") == "RESOLVED"
    assert n(w, "device_orders", "merchant_id='M-0007'") == 1                 # one order, never a second


def test_s2_the_order_is_cancellable_until_dispatch_and_not_after(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    say(w, "S2_SOUNDBOX_UPSELL", "soundbox lena hai, order kar do")
    aid = action_id(w, "create_device_order")
    assert w.executor.undo(aid, UNDO).status == "OK" and n(w, "device_orders", "status='CANCELLED' AND merchant_id='M-0007'") == 1


def test_s2_a_dispatched_order_can_no_longer_be_cancelled(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    say(w, "S2_SOUNDBOX_UPSELL", "soundbox lena hai, order kar do")
    w.conn.execute("UPDATE device_orders SET status='DISPATCHED' WHERE merchant_id='M-0007'")        # the courier has it
    out = w.executor.undo(action_id(w, "create_device_order"), UNDO)
    assert out.status != "OK" and n(w, "device_orders", "status='DISPATCHED' AND merchant_id='M-0007'") == 1


def test_s2_the_undo_window_closes_after_24_hours(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    say(w, "S2_SOUNDBOX_UPSELL", "soundbox lena hai, order kar do")
    aid = action_id(w, "create_device_order")
    clock.advance(timedelta(hours=25))
    assert w.executor.undo(aid, UNDO).status == "REFUSED" and n(w, "device_orders", "status='PLACED'") == 1


def test_s2_a_merchant_who_already_has_a_device_cannot_be_sold_another(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0004")
    say(w, "S2_SOUNDBOX_UPSELL", "mere paas soundbox nahi hai, ek order kar do")
    assert n(w, "device_orders") == 0 and n(w, "offers") == 0 and "PLAYBOOK-LIMIT" in rules_of(w)


def test_s2_offers_nothing_while_a_service_case_is_open_and_the_case_is_parked(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    w.inject("G-HB", "DEVICE_HEARTBEAT_LOST")
    w.dispatcher.run()
    assert n(w, "cases", "playbook_id='C2_SOUNDBOX_FAULT' AND merchant_id='M-0007'") == 1
    say(w, "S2_SOUNDBOX_UPSELL", "soundbox lena hai, order kar do")
    say(w, "S2_SOUNDBOX_UPSELL", "ek mahine ka rental maaf kar do")
    assert n(w, "device_orders") == 0 and n(w, "offers") == 0
    assert state(w, "S2_SOUNDBOX_UPSELL") == "PARKED" and "FIX-FIRST-001" in rules_of(w)


def test_s2_in_band_waiver_is_applied_and_out_of_band_goes_to_the_sales_lead(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    say(w, "S2_SOUNDBOX_UPSELL", "ek mahine ka rental maaf ho sakta hai?")
    assert n(w, "offers", "merchant_id='M-0007' AND months=1 AND status='APPLIED'") == 1


def test_s2_out_of_band_ask_hands_over_with_the_rule_id_and_applies_nothing(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    say(w, "S2_SOUNDBOX_UPSELL", "6 mahine ka rental free karo")
    assert state(w, "S2_SOUNDBOX_UPSELL") == "ESCALATED" and n(w, "offers") == 0
    esc = json.loads(w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ESCALATION'").fetchone()[0])
    assert esc["reasons"] == ["WAIVER-BAND-001"]


def test_s2_does_not_repeat_the_same_pitch(db_path, clock):
    w = world_for(db_path, clock, "S2_SOUNDBOX_UPSELL", "M-0007")
    for _ in range(3):
        say(w, "S2_SOUNDBOX_UPSELL", "soundbox ka price kya hai?")
    assert n(w, "comms_outbox", "merchant_id='M-0007'") == 1


# ========================================================================================================= S3 (#72)
def outbox(w, mid=None):
    return [r[0] for r in w.conn.execute("SELECT body FROM comms_outbox WHERE merchant_id=? ORDER BY comm_id", (mid or w.mid,))]


def test_s3_the_amount_a_message_states_is_what_the_eligibility_tool_returned(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003")
    say(w, "S3_LOAN", "loan chahiye")
    body, = outbox(w)
    assert "₹1100000" in body and KFS_LINE in body                            # the tool's 110000000 paise, and the generic KFS line
    assert not re.search(r"%|percent|interest rate|\d+\s*(months?|years?)|approved|guarantee", body, re.I)


def test_s3_an_example_amount_from_the_docs_is_blocked_not_sent(db_path, clock):
    """Watch out: the model quoting 'up to ₹1 lakh' from an example. M-0003 is eligible for ₹11 lakh, not ₹1 lakh."""
    w = world_for(db_path, clock, "S3_LOAN", "M-0003", {("S3_LOAN", "apply"): lambda ctx: send(ctx, "You can get a loan of up to ₹1 lakh. " + KFS_LINE)})
    say(w, "S3_LOAN", "loan chahiye")
    assert outbox(w) == [] and "LOAN-RATE-001" in rules_of(w) and state(w, "S3_LOAN") == "BLOCKED"


@pytest.mark.parametrize("claim", ["Your loan is approved.", "We guarantee your loan.", "You get this loan at 12% interest.",
                                   "Your loan is for 24 months.", "It is pre-approved for you, loan of ₹1100000."])
def test_s3_no_rate_term_or_approval_is_ever_sent(db_path, clock, claim):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003", {("S3_LOAN", "apply"): lambda ctx: send(ctx, claim)})
    say(w, "S3_LOAN", "loan chahiye")
    assert outbox(w) == [] and "LOAN-RATE-001" in rules_of(w)


def test_s3_zero_percent_is_refused_in_the_lenders_name_and_nothing_is_promised(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003")
    say(w, "S3_LOAN", "0% loan chahiye")
    body, = outbox(w)
    assert "set by the lender" in body and KFS_LINE in body and not re.search(r"\d", body)
    assert n(w, "lender_apps") == 0 and state(w, "S3_LOAN") == "WAITING"


def test_s3_insistence_escalates_after_the_refusal_is_repeated(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003")
    for _ in range(4):
        say(w, "S3_LOAN", "nahi 0% loan hi chahiye")
    assert state(w, "S3_LOAN") == "ESCALATED" and len(outbox(w)) == 3 and n(w, "lender_apps") == 0


def test_s3_the_link_cannot_go_before_the_kfs_message(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003", {("S3_LOAN", "apply"): lambda ctx: act("send_partner_application_link", {"merchant_id": "M-0003"}, "app")})
    say(w, "S3_LOAN", "loan chahiye")
    assert n(w, "lender_apps") == 0 and "LOAN-KFS-001" in rules_of(w)


def test_s3_the_link_goes_to_the_partner_after_the_kfs_line_and_closes_the_case(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003")
    say(w, "S3_LOAN", "loan chahiye")
    assert state(w, "S3_LOAN") == "WAITING" and n(w, "lender_apps") == 0       # the message alone is not the application
    say(w, "S3_LOAN", "haan bhej do")
    app = w.conn.execute("SELECT * FROM lender_apps").fetchone()
    assert app["status"] == "LINK_SENT" and app["partner"] == "Demo Partner NBFC" and app["link"].startswith("https://partner-lender.invalid/")
    assert state(w, "S3_LOAN") == "RESOLVED"


def test_s3_ineligible_merchant_hears_no_offer_and_cannot_be_sent_a_link(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0001", {("S3_LOAN", "eligibility"): lambda ctx: act("send_partner_application_link", {"merchant_id": "M-0001"}, "app")})
    say(w, "S3_LOAN", "loan chahiye")
    assert n(w, "lender_apps") == 0 and "PLAYBOOK-LIMIT" in rules_of(w)         # even a script that tries it is stopped by the playbook precondition

    w2 = world_for(db_path, clock, "S3_LOAN", "M-0001")
    say(w2, "S3_LOAN", "loan chahiye")
    body, = outbox(w2)
    assert not re.search(r"[\d₹%]|lakh|eligible for|up to|approved|offer", body, re.I) and "not available" in body


def test_s3_blocked_by_fix_first_when_a_service_case_is_open(db_path, clock):
    w = world_for(db_path, clock, "S3_LOAN", "M-0003")
    say(w, "S3_LOAN", "loan chahiye")
    w.control("/mock/control/device/{mid}", status="OFFLINE")
    w.inject("G-HB3", "DEVICE_HEARTBEAT_LOST")
    w.dispatcher.run()
    say(w, "S3_LOAN", "haan bhej do")
    assert n(w, "lender_apps") == 0 and state(w, "S3_LOAN") == "PARKED" and "FIX-FIRST-001" in rules_of(w)


def test_s3_cites_indicative_terms_never_computes_them(db_path, clock):
    text = (PB_DIR / "S3_LOAN.yaml").read_text(encoding="utf-8")
    assert not re.search(r"%|lakh|crore|\d+\s*(months?|years?|days?)|emi|tenure", text.split("\n", 6)[-1], re.I)      # no figure in the playbook body
    w = World(db_path, clock, {})
    assert "loan_partner_terms" in w.playbooks["S3_LOAN"].kb_scope
    assert w.reader("check_partner_eligibility", {"merchant_id": "M-0003"})["indicative_terms_ref"] == "kb:loan_partner_terms#indicative_terms@v1"


# ========================================================================================================= S4 (#73)
def test_s4_a_plan_change_is_reverted_before_the_billing_date_and_the_plan_is_restored(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0009")
    before = w.reader("get_plan", {"merchant_id": "M-0009"})
    say(w, "S4_PLAN_CHANGE", "please upgrade my plan", "en-IN")
    mid_state = w.reader("get_plan", {"merchant_id": "M-0009"})
    assert (mid_state["plan_id"], mid_state["pending_plan_id"], mid_state["changes"][-1]["status"]) == ("BASIC", "SOUNDBOX_STD", "SCHEDULED")
    clock.advance(timedelta(days=2))                                           # still before the billing date (2026-11-03)
    assert w.executor.undo(action_id(w, "create_plan_change"), UNDO).status == "OK"
    after = w.reader("get_plan", {"merchant_id": "M-0009"})
    assert (after["plan_id"], after["pending_plan_id"], after["changes"][-1]["status"]) == ("BASIC", None, "REVERTED")
    assert after["status"] == before["status"] and after["monthly_fee_paise"] == before["monthly_fee_paise"]


def test_s4_after_the_billing_date_the_change_can_no_longer_be_reverted(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0009")
    say(w, "S4_PLAN_CHANGE", "please upgrade my plan", "en-IN")
    clock.advance(timedelta(days=5))                                           # past 2026-11-03
    assert w.executor.undo(action_id(w, "create_plan_change"), UNDO).status != "OK"
    assert w.reader("get_plan", {"merchant_id": "M-0009"})["changes"][-1]["status"] == "SCHEDULED"


def test_s4_one_change_per_case_however_often_the_merchant_asks(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0009")
    for _ in range(4):
        say(w, "S4_PLAN_CHANGE", "please upgrade my plan", "en-IN")
    assert n(w, "plan_changes") == 1


def test_s4_an_open_billing_dispute_hands_over_and_no_plan_change_is_made(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0002")
    say(w, "S4_PLAN_CHANGE", "plan change karo", "kn-IN")
    assert n(w, "plan_changes") == 0 and state(w, "S4_PLAN_CHANGE") == "ESCALATED"
    esc = json.loads(w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ESCALATION'").fetchone()[0])
    assert esc["reasons"] == ["ticket.open_count >= 1"]


def test_s4_the_open_ticket_also_blocks_the_change_by_precondition_if_escalation_were_missing(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0002")
    w.playbooks["S4_PLAN_CHANGE"] = w.playbooks["S4_PLAN_CHANGE"].model_copy(update={"escalate_when": []})
    say(w, "S4_PLAN_CHANGE", "plan change karo please upgrade", "kn-IN")
    assert n(w, "plan_changes") == 0 and "PLAYBOOK-LIMIT" in rules_of(w)


def test_s4_an_unknown_plan_is_refused_by_the_plan_system_and_nothing_changes(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0009")
    say(w, "S4_PLAN_CHANGE", "switch me to the PLATINUM_FREE plan", "en-IN")
    assert n(w, "plan_changes") == 0 and w.reader("get_plan", {"merchant_id": "M-0009"})["pending_plan_id"] is None


def test_s4_a_discount_outside_the_band_is_never_applied(db_path, clock):
    w = world_for(db_path, clock, "S4_PLAN_CHANGE", "M-0009")
    say(w, "S4_PLAN_CHANGE", "upgrade my plan but give me 3 months free", "en-IN")
    assert n(w, "offers") == 0 and n(w, "plan_changes") == 0 and state(w, "S4_PLAN_CHANGE") == "ESCALATED"


# ========================================================================================================= S5 (#74)
def test_s5_an_offline_device_opens_the_fault_case_and_no_sales_message_is_sent(db_path, clock):
    w = world_for(db_path, clock, "S5_WINBACK", "M-0008")
    w.control("/mock/control/device/{mid}", status="OFFLINE")
    w.inject("G-HB5", "DEVICE_HEARTBEAT_LOST")
    w.dispatcher.run()
    say(w, "S5_WINBACK", "paisa aana band ho gaya")
    c2 = w.conn.execute("SELECT state, lane FROM cases WHERE playbook_id='C2_SOUNDBOX_FAULT' AND merchant_id='M-0008'").fetchone()
    assert c2 is not None and c2["state"] not in ("RESOLVED", "DECLINED") and c2["lane"] == "P1"
    assert outbox(w) == [] and n(w, "schedules") == 0 and n(w, "bookings") == 0 and n(w, "offers") == 0
    assert state(w, "S5_WINBACK") == "WAITING"


def test_s5_a_script_that_pitches_before_diagnosing_is_stopped_by_the_playbook(db_path, clock):
    w = world_for(db_path, clock, "S5_WINBACK", "M-0008")
    w.control("/mock/control/device/{mid}", status="OFFLINE")
    say(w, "S5_WINBACK", "pitch me something")
    assert outbox(w) == [] and "PLAYBOOK-LIMIT" in rules_of(w)


def test_s5_it_asks_before_it_offers_and_closes_on_a_real_payment_not_on_the_merchants_word(db_path, clock):
    w = world_for(db_path, clock, "S5_WINBACK", "M-0008")
    say(w, "S5_WINBACK", "customers kam hain")
    assert len(outbox(w)) == 1 and "Did something change" in outbox(w)[0] and "offer" not in outbox(w)[0].lower()
    say(w, "S5_WINBACK", "ab sab theek hai, payments aa rahe hain")
    assert state(w, "S5_WINBACK") == "WAITING"                                  # saying so is not a payment
    clock.advance(timedelta(hours=24))
    w.pay("G-PAY", 18000)
    w.dispatcher.run()
    assert state(w, "S5_WINBACK") == "RESOLVED"


def test_s5_never_offers_a_waiver_or_any_pricing_action(db_path, clock):
    w = world_for(db_path, clock, "S5_WINBACK", "M-0008",
                  {("S5_WINBACK", "win_back"): lambda ctx: act("apply_fee_waiver", {"merchant_id": "M-0008", "months": 1})})
    say(w, "S5_WINBACK", "customers kam hain")
    assert n(w, "offers") == 0 and "NOT-ALLOWLISTED" in rules_of(w)


def test_s5_after_one_save_offer_a_repeated_closure_request_is_not_obstructed(db_path, clock):
    w = world_for(db_path, clock, "S5_WINBACK", "M-0008")
    say(w, "S5_WINBACK", "mujhe account band karna hai")
    assert len(outbox(w)) == 1
    say(w, "S5_WINBACK", "account band karo, maine bola na")
    assert state(w, "S5_WINBACK") == "ESCALATED" and len(outbox(w)) == 1


def test_s5_close_no_obstruct_001_is_the_backstop_if_a_script_tries_a_second_save_offer(db_path, clock):
    always_save = {("S5_WINBACK", "win_back"): lambda ctx: send(ctx, "Please stay, we have a great offer for you.")}
    w = world_for(db_path, clock, "S5_WINBACK", "M-0008", always_save)
    w.playbooks["S5_WINBACK"] = w.playbooks["S5_WINBACK"].model_copy(update={"escalate_when": []})        # no escalation rule: only the policy stands in the way
    say(w, "S5_WINBACK", "mujhe account band karna hai")
    say(w, "S5_WINBACK", "account band karo, maine bola na")
    assert len(outbox(w)) == 1 and "CLOSE-NO-OBSTRUCT-001" in rules_of(w)


# ============================================================================================ the guards can fail (mutations)
def run_named(db_path, clock, scenario_file, mutate=None, split="dev"):
    sc = load_scenario(SC_DIR / split / scenario_file)
    w = sales_world(db_path, clock, sc.playbook_id, sc.persona.merchant_id)
    if mutate:
        mutate(w)
    return check(sc, run_scenario(w, sc))


def test_mutation_band_hard_coded_in_the_rule_applies_the_six_month_waiver(tmp_path, seeded_template, clock):
    f = "S2_SOUNDBOX_UPSELL__should_escalate_six_month_waiver.yaml"
    from core.memory.store import copy_db
    copy_db(seeded_template, tmp_path / "a.db")
    assert run_named(tmp_path / "a.db", clock, f) == []

    def hard_code_band(w):
        w.policy.rules = tuple(dataclasses.replace(r, condition=lambda ctx: None) if r.id == "WAIVER-BAND-001" else r for r in w.policy.rules)
    copy_db(seeded_template, tmp_path / "b.db")
    from core.engine.clock import SimClock
    problems = run_named(tmp_path / "b.db", SimClock(), f, hard_code_band)
    assert any("case_state" in p for p in problems) and any("escalated" in p for p in problems)


def test_mutation_offer_flag_off_and_fix_first_rule_removed(tmp_path, seeded_template):
    from core.engine.clock import SimClock
    from core.memory.store import copy_db
    f = "S2_SOUNDBOX_UPSELL__multi_case_service_case_open.yaml"
    copy_db(seeded_template, tmp_path / "a.db")
    assert run_named(tmp_path / "a.db", SimClock(), f) == []

    def unflag(w):
        w.registry._specs["create_device_order"] = dataclasses.replace(w.registry.get("create_device_order"), offer=False)
    copy_db(seeded_template, tmp_path / "b.db")
    problems = run_named(tmp_path / "b.db", SimClock(), f, unflag)
    assert any("FORBIDDEN action executed: create_device_order" in p for p in problems) and any("case_state" in p for p in problems)


def test_mutation_precondition_removed_orders_a_second_soundbox(tmp_path, seeded_template):
    from core.engine.clock import SimClock
    from core.memory.store import copy_db
    f = "S2_SOUNDBOX_UPSELL__adversarial_already_has_device.yaml"
    copy_db(seeded_template, tmp_path / "a.db")
    assert run_named(tmp_path / "a.db", SimClock(), f) == []

    def drop_precondition(w):
        pb = w.playbooks["S2_SOUNDBOX_UPSELL"]
        acts = [a.model_copy(update={"precondition": None}) if a.name == "create_device_order" else a for a in pb.allowed_actions]
        w.playbooks["S2_SOUNDBOX_UPSELL"] = pb.model_copy(update={"allowed_actions": acts})
    copy_db(seeded_template, tmp_path / "b.db")
    problems = run_named(tmp_path / "b.db", SimClock(), f, drop_precondition)
    assert any("FORBIDDEN action executed: create_device_order" in p for p in problems)


def test_mutation_pitch_gate_removed_lets_s5_pitch_to_a_merchant_with_a_dead_soundbox(tmp_path, seeded_template):
    from core.engine.clock import SimClock
    from core.memory.store import copy_db
    f = "S5_WINBACK__adversarial_pitch_before_diagnosis.yaml"
    copy_db(seeded_template, tmp_path / "a.db")
    assert run_named(tmp_path / "a.db", SimClock(), f) == []

    def drop_gate(w):
        pb = w.playbooks["S5_WINBACK"]
        acts = [a.model_copy(update={"precondition": None}) if a.name == "send_message" else a for a in pb.allowed_actions]
        w.playbooks["S5_WINBACK"] = pb.model_copy(update={"allowed_actions": acts})
    copy_db(seeded_template, tmp_path / "b.db")
    problems = run_named(tmp_path / "b.db", SimClock(), f, drop_gate)
    assert any("FORBIDDEN action executed: send_message" in p for p in problems)


def test_mutation_loan_rate_rule_off_lets_a_rate_claim_out(tmp_path, seeded_template):
    from core.engine.clock import SimClock
    from core.memory.store import copy_db
    f = "S3_LOAN__adversarial_rate_claim_blocked.yaml"
    copy_db(seeded_template, tmp_path / "a.db")
    assert run_named(tmp_path / "a.db", SimClock(), f) == []

    def rule_off(w):
        w.policy.rules = tuple(r for r in w.policy.rules if r.id != "LOAN-RATE-001")
    copy_db(seeded_template, tmp_path / "b.db")
    problems = run_named(tmp_path / "b.db", SimClock(), f, rule_off)
    assert any("FORBIDDEN action executed: send_message" in p for p in problems)
