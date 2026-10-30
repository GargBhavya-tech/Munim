"""CHECKPOINT #22: a seeded merchant's S1 case runs end to end on the simulated clock with scripted (non-LLM) decisions,
through the REAL playbook loader, diagnose, case engine, policy engine, executor and mocks."""
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.actions.executor import Executor
from core.audit.ledger import Ledger, verify_chain
from core.audit.replay import replay_case
from core.engine.case_facts import replacements_before_case
from core.engine.contact_budget import ContactBudget
from core.engine.diagnose import diagnose, done_check, escalation_reasons
from core.engine.dispatcher import Dispatcher
from core.engine.playbook import load_playbooks
from core.engine.scripted import scripted_decider
from core.engine.timers import TimerService
from core.engine.workflow import Engine
from core.memory.store import connect
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.mocks import create_app
from packs.paytm_merchant.policy.rules import build_engine
from tests.conftest import count
from tests.world import World

ROOT = Path(__file__).resolve().parent.parent
PB_DIR = ROOT / "packs" / "paytm_merchant" / "playbooks"
MID = "M-0006"

SCRIPT = {
    ("S1_ACTIVATION", "kyc"): {"action": "send_kyc_resume_link", "args": {"merchant_id": MID}, "channel": "app"},
    ("S1_ACTIVATION", "bank"): ("retrigger_bank_verification", {"merchant_id": MID}),
    ("S1_ACTIVATION", "device"): ("create_device_replacement", {"merchant_id": MID, "reason": "NO_ANNOUNCEMENT"}),
}


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


def test_s1_runs_end_to_end_from_stalled_signup_to_first_settlement(world):
    clock, conn = world.clock, world.conn

    # T+0: the signup has stalled for 48h. KYC was rejected (blurry photo), bank pending, Soundbox offline.
    world.inject("E-STALL", "SIGNUP_STALLED")
    d = world.dispatcher.run()
    assert [x.opened for x in d] == [True]
    case = world.case()
    assert case.state == "WAITING" and case.step == "kyc"
    assert count(conn, "links", "merchant_id=? AND kind='KYC_RESUME'", (MID,)) == 1   # exactly one resume link
    assert count(conn, "device_replacements", "merchant_id=?", (MID,)) == 0           # fixed in order: not the device yet

    # T+2h: the merchant resubmits and KYC is approved. The same event moves bank AND device forward.
    clock.advance(timedelta(hours=2))
    world.control(f"/mock/control/kyc/{MID}", status="APPROVED")
    world.inject("E-KYC", "KYC_SUBMITTED")
    world.dispatcher.run()
    assert conn.execute("SELECT status FROM bank WHERE merchant_id=?", (MID,)).fetchone()[0] == "VERIFIED"
    assert count(conn, "device_replacements", "merchant_id=? AND status='CREATED'", (MID,)) == 1
    assert world.case().step == "device"

    # T+26h: the replacement arrives and is online. Nothing to do until a customer pays.
    clock.advance(timedelta(hours=24))
    world.control(f"/mock/control/device/{MID}", status="ONLINE")
    world.inject("E-DEV", "DEVICE_DELIVERED")
    world.dispatcher.run()
    case = world.case()
    assert case.state == "WAITING" and case.step == "payment"
    assert count(conn, "device_replacements", "merchant_id=?", (MID,)) == 1           # never a second replacement

    # T+27h: the demo control "customer pays 240 rupees". The payment is not yet settled, so the case is not done.
    clock.advance(timedelta(hours=1))
    paid = world.pay("pay-1", 24000)
    world.dispatcher.run()
    case = world.case()
    assert case.state == "WAITING" and case.step == "settlement"

    # next day 10:00: settlement reaches the bank. All five done-fields are true, so the case resolves.
    clock.advance(timedelta(hours=23))
    world.advance_settlement(paid["settlement_id"], "settle-1")
    world.dispatcher.run()
    case = world.case()
    assert case.state == "RESOLVED"

    # nothing was done twice
    assert count(conn, "links", "merchant_id=? AND kind='KYC_RESUME'", (MID,)) == 1
    assert count(conn, "device_replacements", "merchant_id=?", (MID,)) == 1
    assert conn.execute("SELECT attempts FROM bank WHERE merchant_id=?", (MID,)).fetchone()[0] == 1

    # the audit trail is intact, attributable to one policy bundle, and replays offline to the same answer
    assert verify_chain(conn).ok
    r = replay_case(conn, case.case_id)
    assert r.state["status"] == "RESOLVED" and r.chain.ok
    intents = [row for row in conn.execute("SELECT policy_hash FROM ledger WHERE kind='ACTION_INTENT'")]
    assert intents and {row[0] for row in intents} == {world.policy.policy_hash}
    acts = [a["action"] for a in r.state["actions"]]
    assert acts.count("send_kyc_resume_link") == 1 and acts.count("create_device_replacement") == 1


def test_a_duplicate_webhook_does_not_act_twice(world):
    world.inject("E-STALL", "SIGNUP_STALLED")
    world.dispatcher.run()
    world.conn.execute("DELETE FROM meta WHERE key='dispatch_cursor'")   # replay the whole event log, as a restarted backbone would
    world.dispatcher.run()
    assert count(world.conn, "links", "merchant_id=? AND kind='KYC_RESUME'", (MID,)) == 1


def test_the_whole_run_survives_timeout_after_commit_chaos(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    w.http.post("/mock/chaos", json={"system": "*", "mode": "timeout_after_commit", "rate": 0.5})
    w.http.post("/mock/chaos", json={"system": "*", "mode": "duplicate_webhook", "rate": 0.5})
    w.inject("E-STALL", "SIGNUP_STALLED")
    for _ in range(6):
        w.dispatcher.run()
        w.conn.execute("DELETE FROM meta WHERE key='dispatch_cursor'")
        w.engine.recover()
    assert count(w.conn, "links", "merchant_id=? AND kind='KYC_RESUME'", (MID,)) == 1
    assert verify_chain(w.conn).ok


def test_own_replacement_in_transit_does_not_escalate_on_the_next_event(world):
    """Regression: S1 escalated to a human on an unrelated event while ITS OWN replacement was still on the way."""
    world.inject("E1", "SIGNUP_STALLED")
    world.dispatcher.run()
    world.clock.advance(timedelta(hours=2))
    world.control(f"/mock/control/kyc/{MID}", status="APPROVED")
    world.inject("E2", "KYC_SUBMITTED")
    world.dispatcher.run()
    assert count(world.conn, "device_replacements", "merchant_id=?", (MID,)) == 1
    world.clock.advance(timedelta(hours=3))
    world.inject("E3", "PAYMENT_FAILED")
    world.dispatcher.run()
    assert world.case().state == "WAITING"
    assert count(world.conn, "device_replacements", "merchant_id=?", (MID,)) == 1


def test_a_replacement_made_before_the_case_still_counts_as_a_repeat(world):
    """The rule must still do its job: a Soundbox already replaced earlier and offline again goes to a human."""
    from core.engine.workflow import Event
    case = world.engine.open_case("M-0012", "S1_ACTIVATION")      # M-0012 had two replacements in the last 30 days
    assert replacements_before_case(world.reader, world.clock, case) == 2
    world.conn.execute("UPDATE devices SET status='OFFLINE' WHERE merchant_id='M-0012'")
    reasons = escalation_reasons(world.playbooks["S1_ACTIVATION"], world.reader, "M-0012",
                                 {"touches": 0, "replacements_last_30d": replacements_before_case(world.reader, world.clock, case)})
    assert any("replacements_last_30d" in r for r in reasons)


def test_playbook_max_per_case_and_precondition_are_actually_enforced(world):
    """Regression: the playbook said max_per_case: 1 and a precondition, and nothing enforced either."""
    from core.engine.diagnose import action_permitted
    pb = world.playbooks["S1_ACTIVATION"]
    reader = world.reader
    # M-0006: KYC rejected, bank pending, device offline. A replacement needs bank done first.
    ok, why = action_permitted(pb, "create_device_replacement", reader, MID, {}, prior_count=0)
    assert not ok and why.startswith("PRECONDITION")
    # once the bank is verified and the device is offline it is allowed, once
    world.control(f"/mock/control/kyc/{MID}", status="APPROVED")
    world.control(f"/mock/control/bank/{MID}", status="VERIFIED")
    assert action_permitted(pb, "create_device_replacement", reader, MID, {}, prior_count=0) == (True, None)
    ok, why = action_permitted(pb, "create_device_replacement", reader, MID, {}, prior_count=1)
    assert not ok and why.startswith("MAX_PER_CASE")
    assert action_permitted(pb, "send_message", reader, MID, {}, prior_count=9) == (True, None)   # unconstrained actions pass


def test_a_premature_replacement_is_declined_and_ledgered_not_executed(db_path, clock):
    """The scripted decider proposes the replacement at the device step, but only when the bank step is done does the playbook allow it."""
    w = World(db_path, clock, {("S1_ACTIVATION", "kyc"): ("create_device_replacement", {"merchant_id": MID, "reason": "X"})})
    w.inject("E1", "SIGNUP_STALLED")
    w.dispatcher.run()
    assert count(w.conn, "device_replacements", "merchant_id=?", (MID,)) == 0
    assert w.case().state == "WAITING"
    row = w.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY' AND payload_json LIKE '%PLAYBOOK-LIMIT%'").fetchone()
    assert row is not None and "PRECONDITION" in row[0]
