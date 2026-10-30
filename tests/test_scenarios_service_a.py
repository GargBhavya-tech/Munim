"""Every dev and held-out scenario of C1, C3, C4, C5 runs through the real World and scenario runner; plus mutation tests that show
the scenarios FAIL when a guard is removed (a scenario that cannot fail proves nothing)."""
import re
import shutil
from pathlib import Path

import pytest

from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from tests import world as world_mod
from tests.scenario_runner import check, run_scenario
from tests.test_service_playbooks_a import IDS, build_world
from tests.world import PB_DIR

SC_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios"


def _mine():
    out = []
    for split in ("dev", "heldout"):
        for pid in IDS:
            for p in sorted((SC_DIR / split).glob(f"{pid}__*.yaml")):
                out.append(load_scenario(p))
    return out


SCENARIOS = _mine()
BY_ID = {s.id: s for s in SCENARIOS}


def run(sc, db_path, clock):
    world = build_world(db_path, clock, sc.persona.merchant_id)
    return world, run_scenario(world, sc)


def test_each_playbook_meets_the_supported_bar_and_covers_every_family():
    cov = coverage(SCENARIOS)
    for pid in IDS:
        assert meets_supported_bar(cov[pid]), (pid, cov[pid])
        assert cov[pid]["families"] == ["adversarial", "happy", "multi_case", "noise", "should_escalate"], (pid, cov[pid])


def test_scenario_ids_are_unique_and_prefixed_with_their_playbook():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    for s in SCENARIOS:
        assert s.id.startswith(s.playbook_id.split("_")[0] + "-"), s.id


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    world, res = run(sc, db_path, clock)
    assert check(sc, res) == []


# ---------------------------------------------------------------------------------------------------------------------
# facts about particular scenarios that the generic checker does not look at
# ---------------------------------------------------------------------------------------------------------------------
def test_refund_scenarios_never_leave_a_chat_yes_as_confirmation(db_path, clock):
    sc = BY_ID["C4-dev-adversarial-chat-yes-is-not-confirmation"]
    world, res = run(sc, db_path, clock)
    assert res.case_state == "WAITING"
    assert world.conn.execute("SELECT status FROM refunds").fetchall()[0][0] == "AWAITING_MERCHANT_CONFIRM"
    assert world.conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 1


def test_the_number_in_chat_scenario_is_refused_by_kyc_no_chat_001_and_the_bank_row_is_unchanged(db_path, clock):
    sc = BY_ID["C5-heldout-adversarial-account-number-echo"]
    world = build_world(db_path, clock, sc.persona.merchant_id)
    before = tuple(world.conn.execute("SELECT account_last4, ifsc, status FROM bank WHERE merchant_id='M-0004'").fetchone())
    res = run_scenario(world, sc)
    assert res.refused.get("send_message") == 1 and res.executed.get("send_message") is None
    assert tuple(world.conn.execute("SELECT account_last4, ifsc, status FROM bank WHERE merchant_id='M-0004'").fetchone()) == before


@pytest.mark.parametrize("sid", ["C1-heldout-escalate-risk-hold", "C1-dev-adversarial-release-now"])
def test_the_risk_hold_scenarios_leave_the_settlement_held_and_touch_nothing(sid, db_path, clock):
    sc = BY_ID[sid]
    world, res = run(sc, db_path, clock)
    assert res.escalated and res.executed == {} or set(res.executed) <= {"get_settlements"}
    assert world.conn.execute("SELECT status FROM settlements WHERE settlement_id='S-0060'").fetchone()[0] == "HELD_RISK"
    assert world.conn.execute("SELECT COUNT(*) FROM schedules WHERE merchant_id='M-0002'").fetchone()[0] == 0


# ---------------------------------------------------------------------------------------------------------------------
# mutation proofs: remove a guard and the scenario that depends on it must fail
# ---------------------------------------------------------------------------------------------------------------------
def _mutated_pb_dir(tmp_path, monkeypatch, pb_file, *edits):
    d = tmp_path / "playbooks"
    d.mkdir()
    for name in ("C1_SETTLEMENT_DELAY", "C2_SOUNDBOX_FAULT", "C3_PAYMENT_FAILED", "C4_MERCHANT_REFUND", "C5_DETAIL_UPDATE", "S1_ACTIVATION"):
        shutil.copy(PB_DIR / f"{name}.yaml", d / f"{name}.yaml")
    f = d / f"{pb_file}.yaml"
    text = f.read_text()
    for old, new in edits:
        assert old in text, f"mutation target not found in {pb_file}: {old!r}"
        text = text.replace(old, new)
    f.write_text(text)
    monkeypatch.setattr(world_mod, "PB_DIR", d)


def _problems(sc, db_path, clock):
    world = build_world(db_path, clock, sc.persona.merchant_id)
    return check(sc, run_scenario(world, sc))


def test_unmutated_copy_of_the_playbooks_passes_the_scenarios_used_by_the_mutation_tests(tmp_path, monkeypatch, db_path, clock):
    _mutated_pb_dir(tmp_path, monkeypatch, "C1_SETTLEMENT_DELAY", ("max_per_case: 1", "max_per_case: 1"))
    for sid in ("C1-heldout-escalate-risk-hold", "C1-dev-noise-duplicate-webhook-bank-failed"):
        assert _problems(BY_ID[sid], db_path, clock) == []


MUTATIONS = [
    # (name, playbook, text to remove, replacement, scenario that must now fail)
    ("C1 no escalation on a risk hold", "C1_SETTLEMENT_DELAY", [("  - \"settlement.status == 'HELD_RISK'\"\n  - \"risk.has_open_hold\"\n", "")],
     "C1-dev-escalate-small-risk-hold"),
    ("C1 link may be sent twice", "C1_SETTLEMENT_DELAY", [("send_bank_update_link: {max_per_case: 1, ", "send_bank_update_link: {")], "C1-dev-noise-duplicate-webhook-bank-failed"),
    ("C1 no amount limit", "C1_SETTLEMENT_DELAY", [("  - \"settlement.amount_paise >= 1000000\"\n", "")], "C1-dev-escalate-amount-over-limit"),
    ("C3 dispute step skipped", "C3_PAYMENT_FAILED", [("{name: dispute, done: \"ticket.open_count >= 1\"}", "{name: dispute, done: \"payment.status != 'NONE'\"}")],
     "C3-dev-happy-dispute-waiting"),
    ("C3 no complaint escalation", "C3_PAYMENT_FAILED", [("  - \"intent in ['COMPLAINT', 'WANTS_HUMAN']\"\n", "")], "C3-heldout-escalate-complaint"),
    ("C4 a created request counts as the merchant's confirmation", "C4_MERCHANT_REFUND",
     [("{name: confirmation, done: \"refund.status == 'PROCESSED'\"}", "{name: confirmation, done: \"refund.status != 'NONE'\"}"),
      ("done_check: \"refund.status == 'PROCESSED'\"", "done_check: \"refund.status != 'NONE'\"")],
     "C4-dev-adversarial-chat-yes-is-not-confirmation"),
    ("C5 link step satisfied without a link", "C5_DETAIL_UPDATE", [("{name: link, done: \"touches >= 1\"}", "{name: link, done: \"bank.status == 'VERIFIED'\"}")],
     "C5-dev-happy-healthy-merchant-link"),
    ("C5 no pressure escalation", "C5_DETAIL_UPDATE", [("  - \"intent in ['COMPLAINT', 'WANTS_HUMAN']\"\n", "")], "C5-heldout-escalate-complaint-pressure"),
]


@pytest.mark.parametrize("name,pb,edits,sid", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_removing_a_guard_makes_its_scenario_fail(name, pb, edits, sid, tmp_path, monkeypatch, db_path, clock):
    _mutated_pb_dir(tmp_path, monkeypatch, pb, *edits)
    assert _problems(BY_ID[sid], db_path, clock) != [], f"mutation '{name}' went unnoticed by {sid}"


def test_disabling_kyc_no_chat_makes_the_number_in_chat_scenario_fail(monkeypatch, db_path, clock):
    from packs.paytm_merchant.policy import rules
    real = world_mod.build_engine
    monkeypatch.setattr(world_mod, "build_engine", lambda *a, **k: real(*a, **{**k, "rules": [r for r in rules.RULES if r.id != "KYC-NO-CHAT-001"]}))
    assert _problems(BY_ID["C5-heldout-adversarial-account-number-echo"], db_path, clock) != []


def test_disabling_money_limit_lets_a_large_refund_through_the_policy_engine(monkeypatch, db_path, clock):
    from core.actions.types import ActionIntent
    from packs.paytm_merchant.policy import rules
    real = world_mod.build_engine
    monkeypatch.setattr(world_mod, "build_engine", lambda *a, **k: real(*a, **{**k, "rules": [r for r in rules.RULES if r.id != "MONEY-LIMIT-001"]}))
    world = build_world(db_path, clock, "M-0004")
    out = world.executor.submit(ActionIntent("create_refund_request", {"merchant_id": "M-0004", "payment_id": "P-1818", "amount_paise": 1000000}))
    assert out.status != "ESCALATE"      # without the rule the policy no longer escalates (the mock then refuses > payment): the rule is what held it


LARGE_REFUND_RULE = ("  - \"payment.latest_amount_paise >= 1000000\"\n", "")


def test_a_large_refund_still_escalates_through_money_limit_001_when_the_playbook_rule_is_removed(tmp_path, monkeypatch, db_path, clock):
    """Defense in depth: the playbook rule and the policy rule are independent locks."""
    _mutated_pb_dir(tmp_path, monkeypatch, "C4_MERCHANT_REFUND", LARGE_REFUND_RULE)
    sc = BY_ID["C4-heldout-escalate-large-amount"]
    world = build_world(db_path, clock, sc.persona.merchant_id)
    res = run_scenario(world, sc)
    assert check(sc, res) == [] and res.refused == {"create_refund_request": 1} and res.executed.get("create_refund_request") is None
    assert "MONEY-LIMIT-001" in str(world.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY' AND payload_json LIKE '%MONEY-LIMIT-001%'").fetchone()[0])


def test_with_both_locks_removed_a_large_refund_request_is_created(tmp_path, monkeypatch, db_path, clock):
    from packs.paytm_merchant.policy import rules
    _mutated_pb_dir(tmp_path, monkeypatch, "C4_MERCHANT_REFUND", LARGE_REFUND_RULE)
    real = world_mod.build_engine
    monkeypatch.setattr(world_mod, "build_engine", lambda *a, **k: real(*a, **{**k, "rules": [r for r in rules.RULES if r.id != "MONEY-LIMIT-001"]}))
    assert _problems(BY_ID["C4-heldout-escalate-large-amount"], db_path, clock) != []
