from pathlib import Path

import pytest

from core.eval.scenario import load_scenarios
from tests.checkpoint_script import SCRIPT as S1_SCRIPT
from tests.scenario_runner import check, run_scenario
from tests.world import World

SC_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios"
S1 = [s for s in load_scenarios(SC_DIR) if s.playbook_id == "S1_ACTIVATION"]


def test_example_scenarios_exist_for_both_splits_or_at_least_dev():
    assert {s.id for s in S1} >= {"S1-dev-happy-stalled-signup", "S1-dev-escalate-name-mismatch"}


@pytest.mark.parametrize("sc", S1, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    world = World(db_path, clock, S1_SCRIPT, mid=sc.persona.merchant_id)
    assert check(sc, run_scenario(world, sc)) == []


def test_the_checker_actually_fails_on_a_wrong_expectation(db_path, clock):
    """A scenario that expects the wrong end state must be reported, or the runner proves nothing."""
    sc = next(s for s in S1 if s.id == "S1-dev-escalate-name-mismatch").model_copy(deep=True)
    sc.expected.case_state = "RESOLVED"
    sc.expected.escalation.expected = False
    sc.expected.forbidden_actions = ["send_kyc_resume_link"]       # it DID execute once at the first step
    world = World(db_path, clock, S1_SCRIPT, mid=sc.persona.merchant_id)
    problems = check(sc, run_scenario(world, sc))
    assert any("case_state" in p for p in problems) and any("escalated" in p for p in problems) and any("FORBIDDEN" in p for p in problems)
