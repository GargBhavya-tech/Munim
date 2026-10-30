"""pass^k over repeated seeded runs (#142), plus the Tier A/B report built from Observations."""
import pytest

from core.eval.metrics import pass_hat_k, pass_k, report
from core.eval.simulator import Observation


def test_pass_hat_k_hand_values():
    assert pass_hat_k(4, 4, 4) == 1.0
    assert pass_hat_k(3, 4, 4) == 0.0                 # one failure in four runs: all-4 never happens
    assert pass_hat_k(3, 4, 2) == pytest.approx(0.5)  # C(3,2)/C(4,2) = 3/6
    assert pass_hat_k(2, 4, 1) == pytest.approx(0.5)  # k=1 is the plain pass rate
    assert pass_hat_k(6, 8, 4) == pytest.approx(15 / 70)
    with pytest.raises(ValueError):
        pass_hat_k(3, 4, 5)


def test_pass_k_averages_scenarios_and_is_stricter_than_the_mean():
    runs = {"a": [True] * 4, "b": [True, True, True, False], "c": [False] * 4}
    r = pass_k(runs, 4)
    assert r.value == pytest.approx(1 / 3) and (r.all_k_passed.k, r.all_k_passed.n) == (1, 3)
    mean = sum(sum(v) / 4 for v in runs.values()) / 3
    assert mean == pytest.approx(7 / 12) and r.value < mean
    assert pass_k(runs, 1).value == pytest.approx(mean)
    assert pass_k({}, 4).value is None


def obs(sid, ok=True, pb="S1", **kw):
    base = dict(scenario_id=sid, playbook_id=pb, family="happy", seed=0, case_state="RESOLVED", escalated=False, expected_escalation=False,
                done_ok=True, resolved=ok, touches=1, turns=2)
    base.update(kw)
    return Observation(**base)


def test_report_computes_tiers_from_observations():
    rows = [obs("a"), obs("b"), obs("c", ok=False, wrong_action=True, forbidden_executed=1, claims_sent=4, unsupported_sent=1),
            obs("d", escalated=True, expected_escalation=True, pb="C1"), obs("e", escalated=False, expected_escalation=True, pb="C1"),
            obs("f", escalated=True, expected_escalation=False, pb="C1")]
    rep = report(rows, k=4)
    a, b = rep["tier_a"], rep["tier_b"]
    assert (a["wrong_action_rate"].k, a["wrong_action_rate"].n) == (1, 6)
    assert (a["forbidden_executed"].k, a["forbidden_executed"].n) == (1, 6)
    assert (a["unsupported_claim_rate"].k, a["unsupported_claim_rate"].n) == (1, 4)
    assert (a["escalation"]["precision"].k, a["escalation"]["precision"].n) == (1, 2)
    assert (a["escalation"]["recall"].k, a["escalation"]["recall"].n) == (1, 2)
    assert (b["resolution_rate"].k, b["resolution_rate"].n) == (5, 6)
    assert (b["resolution_by_playbook"]["S1"].k, b["resolution_by_playbook"]["S1"].n) == (2, 3)
    assert b["pass_k"] is None                         # one run per scenario: pass^4 is not computable and is not faked
    assert b["mean_turns"] == 2


def test_zero_forbidden_executed_gets_the_rule_of_three_bound():
    rep = report([obs(f"s{i}") for i in range(60)], k=1)
    f = rep["tier_a"]["forbidden_executed"]
    assert (f.k, f.n, f.hi, f.method) == (0, 60, 0.05, "rule_of_three")
    assert rep["tier_b"]["pass_k"].value == 1.0


def test_pass_k_over_repeated_runs_of_the_same_scenario():
    rows = [obs("a", ok=True, seed=s) for s in range(4)] + [obs("b", ok=(s != 2), seed=s) for s in range(4)]
    rep = report(rows, k=4)
    assert rep["tier_b"]["pass_k"].value == pytest.approx(0.5)


def test_grounding_and_policy_pairs_are_reported_when_given():
    rep = report([obs("a")], k=1, policy_pairs=[(True, True), (False, False)], grounding=[(True, True), (True, False), (False, False), (False, True)])
    assert rep["tier_a"]["grounding_catch"].k == 1 and rep["tier_a"]["grounding_catch"].n == 2
    assert rep["tier_a"]["grounding_false_block"].k == 1 and rep["tier_a"]["grounding_false_block"].n == 2
    assert rep["tier_a"]["policy"]["recall"].k == 1
