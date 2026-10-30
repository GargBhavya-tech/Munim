"""#70 S1 and #79 C2 reach the supported bar: every dev and held-out scenario runs through the real World and scenario runner with a
scripted decider (the model's stand-in), plus the tickets' own Tests: S1 resolves only when all five fields are true; C2 orders a
replacement only after the checklist, and a second replacement escalates."""
import os
from pathlib import Path

import pytest

from core.engine.scripted import scripted_decider
from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from tests.scenario_runner import check, run_scenario
from tests.world import World

SC_DIR = Path(os.environ.get("MUNIM_SCENARIO_DIR") or Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios")
IDS = ("S1_ACTIVATION", "C2_SOUNDBOX_FAULT")
CHECKLIST = ("Please check three things: the charger is plugged in and the light is on, the SIM or Wi-Fi light is blinking, "
             "and the volume is turned up. Reply when you have checked.")


def mid_args(**extra):
    return lambda ctx: {"merchant_id": ctx.case.merchant_id, **extra}


SCRIPT = {
    ("S1_ACTIVATION", "kyc"): {"action": "send_kyc_resume_link", "args": mid_args(), "channel": "app"},
    ("S1_ACTIVATION", "bank"): ("retrigger_bank_verification", mid_args()),
    ("S1_ACTIVATION", "device"): ("create_device_replacement", mid_args(reason="NO_ANNOUNCEMENT")),
    ("C2_SOUNDBOX_FAULT", "check_basics"): {"message_en": CHECKLIST},
    ("C2_SOUNDBOX_FAULT", "remote_fix"): ("restart_device", mid_args()),
    ("C2_SOUNDBOX_FAULT", "test_announcement"): ("create_device_replacement", mid_args(reason="NO_ANNOUNCEMENT")),
    ("C2_SOUNDBOX_FAULT", "replace_or_visit"): ("create_device_replacement", mid_args(reason="NO_ANNOUNCEMENT")),
}


def scenarios():
    out = []
    for split in ("dev", "heldout"):
        for pid in IDS:
            out += [load_scenario(p) for p in sorted((SC_DIR / split).glob(f"{pid}__*.yaml"))]
    return out


SCENARIOS = scenarios()


def build(db_path, clock, mid):
    w = World(db_path, clock, None, mid=mid)
    w.engine.decider = scripted_decider(SCRIPT)
    return w


def test_both_playbooks_meet_the_supported_bar_and_cover_every_family():
    cov = coverage(SCENARIOS)
    for pid in IDS:
        assert meets_supported_bar(cov[pid]), (pid, cov[pid])
        assert cov[pid]["families"] == ["adversarial", "happy", "multi_case", "noise", "should_escalate"], (pid, cov[pid])


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    w = build(db_path, clock, sc.persona.merchant_id)
    assert check(sc, run_scenario(w, sc)) == []


# ---- #70 Test: the simulated-clock lifecycle ends RESOLVED only when all five fields are true ----------------------------
def test_s1_never_resolves_before_the_settlement_even_with_everything_else_done(db_path, clock):
    from datetime import timedelta
    w = build(db_path, clock, "M-0006")
    w.inject("L-1", "SIGNUP_STALLED"); w.dispatcher.run()
    w.clock.advance(timedelta(hours=2)); w.control("/mock/control/kyc/{mid}", status="APPROVED")
    w.inject("L-2", "KYC_SUBMITTED"); w.dispatcher.run()
    assert w.case().state != "RESOLVED"                                  # KYC submitted and approved is not activation
    w.clock.advance(timedelta(hours=24)); w.control("/mock/control/device/{mid}", status="ONLINE")
    w.inject("L-3", "DEVICE_DELIVERED"); w.dispatcher.run()
    w.pay("L-pay", 24000); w.dispatcher.run()
    assert w.case().state != "RESOLVED"                                  # paid but not yet settled
    row = w.conn.execute("SELECT settlement_id FROM settlements WHERE merchant_id='M-0006' AND status='PENDING' ORDER BY expected_at DESC").fetchone()
    w.advance_settlement(row[0], "L-adv"); w.dispatcher.run()
    assert w.case().state == "RESOLVED"


# ---- #79 Watch out: replacement before checklist; Test: second replacement escalates ---------------------------------------
def test_c2_replacement_is_refused_before_the_checklist_is_answered(db_path, clock):
    w = World(db_path, clock, None, mid="M-0005")
    w.engine.decider = scripted_decider({("C2_SOUNDBOX_FAULT", "check_basics"): ("create_device_replacement", mid_args(reason="NO_ANNOUNCEMENT"))})
    w.inject("H-1", "DEVICE_HEARTBEAT_LOST"); w.dispatcher.run()
    assert w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE merchant_id='M-0005'").fetchone()[0] == 0
    import json
    refusals = [json.loads(r[0]) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY'")]
    assert any(p["action"] == "create_device_replacement" and "PRECONDITION" in (p.get("reason") or "") for p in refusals)


def test_c2_a_replied_checklist_is_what_unlocks_the_replacement(db_path, clock):
    from core.engine.case_facts import conversational_steps
    w = build(db_path, clock, "M-0005")
    assert conversational_steps(w.playbooks["C2_SOUNDBOX_FAULT"]) == ["check_basics"]
    assert conversational_steps(w.playbooks["S1_ACTIVATION"]) == []          # a reply can never complete an S1 step
    w.inject("H-1", "DEVICE_HEARTBEAT_LOST"); w.dispatcher.run()
    case = w.case("C2_SOUNDBOX_FAULT")
    assert case.step == "check_basics" and w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE merchant_id='M-0005'").fetchone()[0] == 0
    w.inject("H-2", "MERCHANT_MESSAGE", payload={"text": "kiya, band hai"}); w.dispatcher.run()
    assert w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE merchant_id='M-0005' AND status='CREATED'").fetchone()[0] == 1


def test_c2_second_replacement_within_30_days_escalates(db_path, clock):
    from datetime import timedelta
    w = build(db_path, clock, "M-0005")
    w.inject("H-1", "DEVICE_HEARTBEAT_LOST"); w.dispatcher.run()
    w.inject("H-2", "MERCHANT_MESSAGE", payload={"text": "band hai"}); w.dispatcher.run()
    first = w.case("C2_SOUNDBOX_FAULT")
    w.clock.advance(timedelta(hours=24)); w.control("/mock/control/device/{mid}", status="ONLINE")
    w.inject("H-3", "DEVICE_DELIVERED"); w.dispatcher.run()
    assert w.engine.repo.load(first.case_id).state == "RESOLVED"
    w.clock.advance(timedelta(days=5)); w.control("/mock/control/device/{mid}", status="OFFLINE")
    w.inject("H-4", "DEVICE_HEARTBEAT_LOST"); w.dispatcher.run()
    second = w.case("C2_SOUNDBOX_FAULT")
    assert second.case_id != first.case_id and second.state == "ESCALATED"
    assert w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE merchant_id='M-0005'").fetchone()[0] == 1
