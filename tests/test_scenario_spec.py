from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core.eval.scenario import Scenario, coverage, load_scenario, load_scenarios, meets_supported_bar

GOOD = {
    "id": "S1-happy-01", "playbook_id": "S1_ACTIVATION", "family": "happy", "description": "stalled signup recovers",
    "persona": {"merchant_id": "M-0006", "language": "kn-IN", "traits": ["confused"], "hidden_goal": "get paid"},
    "steps": [{"event": "SIGNUP_STALLED"}, {"after_hours": 2, "control": {"path": "/mock/control/kyc/{mid}", "body": {"status": "APPROVED"}}}],
    "expected": {"case_state": "RESOLVED", "done_fields": {"kyc.status": "APPROVED"}, "escalation": {"expected": False}},
}


def test_a_good_scenario_validates():
    sc = Scenario.model_validate(GOOD)
    assert sc.expected.max_touches == 4 and sc.steps[1].after_hours == 2


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d["steps"].append({"event": "SIGNUP_STALLED", "pay": 5}), "two actions in one step"),
    (lambda d: d["steps"].append({"after_hours": 1}), "no action"),
    (lambda d: d["expected"].__setitem__("case_state", "DONE"), "unknown state"),
    (lambda d: d.__setitem__("family", "vibes"), "unknown family"),
    (lambda d: d.__setitem__("steps", []), "no steps"),
    (lambda d: d.__setitem__("surprise", 1), "extra field"),
])
def test_bad_scenarios_are_rejected(mutate, why):
    d = yaml.safe_load(yaml.safe_dump(GOOD))
    mutate(d)
    with pytest.raises(ValidationError):
        Scenario.model_validate(d)


def test_directory_and_file_name_rules(tmp_path):
    (tmp_path / "dev").mkdir()
    (tmp_path / "heldout").mkdir()
    (tmp_path / "dev" / "S1_ACTIVATION__happy_01.yaml").write_text(yaml.safe_dump(GOOD))
    (tmp_path / "heldout" / "S1_ACTIVATION__happy_02.yaml").write_text(yaml.safe_dump({**GOOD, "id": "S1-happy-02"}))
    scs = load_scenarios(tmp_path)
    assert [s.split for s in scs] == ["dev", "heldout"]
    assert coverage(scs)["S1_ACTIVATION"] == {"dev": 1, "heldout": 1, "families": ["happy"]}
    assert not meets_supported_bar(coverage(scs)["S1_ACTIVATION"])
    (tmp_path / "dev" / "wrongname.yaml").write_text(yaml.safe_dump({**GOOD, "id": "x"}))
    with pytest.raises(ValueError, match="must start with"):
        load_scenario(tmp_path / "dev" / "wrongname.yaml")


def test_duplicate_ids_rejected(tmp_path):
    (tmp_path / "dev").mkdir()
    (tmp_path / "heldout").mkdir()
    for n in ("a", "b"):
        (tmp_path / "dev" / f"S1_ACTIVATION__{n}.yaml").write_text(yaml.safe_dump(GOOD))
    with pytest.raises(ValueError, match="duplicate"):
        load_scenarios(tmp_path)
