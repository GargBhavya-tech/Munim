"""Wilson intervals, rule of three and the frozen-set manifest check (#142). Expected values are computed by hand, not by the code."""
import shutil
from pathlib import Path

import pytest

from core.eval import metrics
from core.eval.metrics import FrozenSetTampered, load_frozen, precision_recall, rate, report, verify_manifest, wilson

FROZEN = Path(__file__).resolve().parent.parent / "eval" / "frozen"


def test_wilson_hand_computed_values():
    # 57/60: centre 0.92294, half 0.05998 -> [0.863, 0.983] (Build Map)
    lo, hi = wilson(57, 60)
    assert (round(lo, 3), round(hi, 3)) == (0.863, 0.983)
    # 8/10: the textbook Wilson interval [0.490, 0.943]; the normal approximation would give [0.552, 1.048]
    lo, hi = wilson(8, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.490, 0.943)
    # 1/20: [0.0089, 0.2361]
    lo, hi = wilson(1, 20)
    assert (round(lo, 4), round(hi, 4)) == (0.0089, 0.2361)


def test_small_n_never_leaves_0_1_unlike_the_normal_approximation():
    lo, hi = wilson(9, 10)
    assert hi <= 1.0 and lo > 0.55                    # normal approx: 0.9 +- 0.186 -> upper 1.086
    assert wilson(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        wilson(11, 10)


def test_rule_of_three_for_zeros():
    r = rate(0, 60)
    assert (r.value, r.lo, r.hi, r.method) == (0.0, 0.0, 0.05, "rule_of_three")      # 3/60, not Wilson's 0.0602
    assert rate(60, 60).lo == pytest.approx(0.95)
    assert rate(0, 0).method == "none" and rate(0, 0).value is None
    r = rate(57, 60)
    assert r.method == "wilson" and r.value == pytest.approx(0.95)


def test_precision_recall_with_intervals():
    pairs = [(True, True)] * 8 + [(True, False)] * 2 + [(False, True)] * 1 + [(False, False)] * 5
    pr = precision_recall(pairs)
    assert (pr["precision"].k, pr["precision"].n) == (8, 9) and (pr["recall"].k, pr["recall"].n) == (8, 10)
    assert round(pr["recall"].lo, 3) == 0.490 and round(pr["recall"].hi, 3) == 0.943


# ------------------------------------------------------------------------------------------------ frozen manifest
def test_the_real_frozen_sets_verify_and_load():
    assert len(load_frozen("claims")["claims"]) > 0
    assert set(load_frozen("probes")) == {"policy", "escalation"}


@pytest.fixture
def copy_of_frozen(tmp_path):
    root = tmp_path / "frozen"
    shutil.copytree(FROZEN, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


@pytest.mark.parametrize("sabotage", ["edit", "delete", "add", "no_manifest"])
def test_a_tampered_frozen_set_is_refused(copy_of_frozen, sabotage):
    d = copy_of_frozen / "probes"
    if sabotage == "edit":
        f = d / "policy_probes.json"
        f.write_text(f.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif sabotage == "delete":
        (d / "escalation_snapshots.json").unlink()
    elif sabotage == "add":
        (d / "extra.json").write_text("{}", encoding="utf-8")
    else:
        (d / "MANIFEST.sha256").unlink()
    with pytest.raises(FrozenSetTampered):
        load_frozen("probes", root=copy_of_frozen)
    with pytest.raises(FrozenSetTampered):
        verify_manifest(d)


def test_untouched_copy_verifies(copy_of_frozen):
    assert load_frozen("claims", root=copy_of_frozen)["claims"]


def test_metrics_never_writes_into_the_frozen_dirs():
    before = {p: p.stat().st_mtime_ns for p in FROZEN.rglob("*") if p.is_file()}
    load_frozen("claims"), load_frozen("probes")
    assert before == {p: p.stat().st_mtime_ns for p in FROZEN.rglob("*") if p.is_file()}


def test_report_runs_nothing_when_a_check_fails_in_the_caller(copy_of_frozen):
    """Loading is the gate: a caller that goes through load_frozen cannot reach the data after tampering."""
    f = copy_of_frozen / "claims" / "claims.jsonl"
    f.write_text(f.read_text(encoding="utf-8").replace("true", "false", 1), encoding="utf-8")
    with pytest.raises(FrozenSetTampered):
        load_frozen("claims", root=copy_of_frozen)
