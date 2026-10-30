"""#142 `munim eval --split dev`: tiers with Wilson intervals from recorded results; stale results refused; the held-out split is never run here."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.cli import main as cli_main
from core.eval import report_cli as R
from core.eval.matrix import config_hash, empty_store
from core.eval.metrics import wilson
from core.eval.scenario import load_scenarios

ROOT = Path(__file__).resolve().parent.parent
SC = load_scenarios(ROOT / "packs" / "paytm_merchant" / "scenarios")
DEV = [s for s in SC if s.split == "dev"]
H = "abc123"


def store(passing, failing=(), runs=1, chash=H):
    """A results store where `passing` scenarios passed every run, `failing` failed every run, and the rest were never run."""
    st = empty_store(chash)
    st["last_run"] = {"run": runs, "recorded_at": "2026-10-30T10:00:00+00:00", "config_hash": chash}
    for s in DEV:
        if s.id in passing:
            st["scenarios"][s.id] = {"attempts": [True] * runs, "last_problem": ""}
        elif s.id in failing:
            st["scenarios"][s.id] = {"attempts": [False] * runs, "last_problem": "x"}
    return st


ALL = {s.id for s in DEV}


def test_the_overall_rate_uses_the_wilson_interval_exactly():
    bad = {s.id for s in DEV[:3]}
    rep = R.build_report(SC, store(ALL - bad, bad), "dev", H)
    r = rep["tier_b"]["scenarios passed (latest run)"]
    n = len(DEV)
    lo, hi = wilson(n - 3, n)
    assert (r.k, r.n, r.method) == (n - 3, n, "wilson") and r.lo == pytest.approx(lo) and r.hi == pytest.approx(hi)


def test_all_passing_uses_the_rule_of_three_not_a_100_percent_claim():
    rep = R.build_report(SC, store(ALL), "dev", H)
    r = rep["tier_b"]["scenarios passed (latest run)"]
    assert r.method == "rule_of_three" and r.value == 1.0 and r.lo == pytest.approx(1 - 3 / len(DEV))


def test_scenarios_that_were_not_run_are_listed_and_not_counted_as_passes_or_failures():
    some = {s.id for s in DEV[:10]}
    rep = R.build_report(SC, store(some), "dev", H)
    assert rep["tier_b"]["scenarios passed (latest run)"].n == 10 and len(rep["not_run"]) == len(DEV) - 10


def test_stale_results_are_refused_and_show_no_numbers():
    rep = R.build_report(SC, store(ALL, chash="old"), "dev", H)
    assert rep["stale"] and "stale" in rep["error"] and "tier_b" not in rep
    assert "ERROR" in R.render(rep)


def test_the_heldout_split_cannot_be_evaluated_here(monkeypatch):
    with pytest.raises(ValueError, match="Gate 4"):
        R.build_report(SC, store(set()), "heldout", H)
    monkeypatch.setattr(R, "run_dev_scenarios", lambda p: pytest.fail("must not run anything for the held-out split"))
    assert R.main(["--split", "heldout"]) == 2


def test_pass_k_needs_k_recorded_runs_and_is_exact_when_it_has_them():
    one = R.build_report(SC, store(ALL, runs=1), "dev", H)
    assert one["tier_b"]["pass_k"] is None and one["tier_b"]["min_attempts"] == 1
    assert "NOT AVAILABLE" in R.render(one)
    four = R.build_report(SC, store(ALL, runs=4), "dev", H)
    pk = four["tier_b"]["pass_k"]
    assert pk.k == 4 and pk.value == 1.0 and pk.all_k_passed.k == len(DEV)


def test_what_this_command_cannot_measure_is_said_so_never_shown_as_zero():
    text = R.render(R.build_report(SC, store(ALL), "dev", H))
    for row in R.NOT_HERE:
        assert row in text
    assert text.count("NOT MEASURED HERE") >= len(R.NOT_HERE)
    assert "TIER A" in text and "TIER B" in text and "TIER C" in text


def test_by_playbook_rows_sum_to_the_overall_count():
    rep = R.build_report(SC, store(ALL), "dev", H)
    assert sum(r.n for r in rep["tier_b"]["by_playbook"].values()) == rep["tier_b"]["scenarios passed (latest run)"].n


def test_munim_eval_is_wired_into_the_cli_and_needs_no_database(tmp_path, capsys):
    path = tmp_path / "res.json"
    path.write_text(json.dumps(store(ALL, chash="old")), encoding="utf-8")
    code = cli_main(["--db", str(tmp_path / "nonexistent.db"), "eval", "--no-run", "--results", str(path)])
    out = capsys.readouterr().out
    assert code == 1 and "stale" in out and not (tmp_path / "nonexistent.db").exists()


def test_a_current_results_file_prints_all_three_tiers_through_the_cli(tmp_path, capsys):
    path = tmp_path / "res.json"
    path.write_text(json.dumps(store(ALL, runs=4, chash=config_hash(ROOT))), encoding="utf-8")
    assert cli_main(["eval", "--no-run", "--results", str(path)]) == 0
    out = capsys.readouterr().out
    assert "TIER A" in out and "TIER B" in out and "TIER C" in out and "pass^4" in out


@pytest.mark.parametrize("split,other", [("dev", "-heldout-"), ("heldout", "-dev-")])
def test_scenario_split_option_collects_only_that_splits_scenario_tests(split, other):
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_lender_pack.py", "--collect-only", "-o", "addopts=", "--scenario-split", split],
                       cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONUTF8": "1"})
    ids = [line for line in r.stdout.splitlines() if "<Function test_scenario_final_state" in line]
    assert ids and all(f"-{split}-" in i for i in ids) and not any(other in i for i in ids), r.stdout[-800:]
