"""#146 wiring: `munim heldout` plays the held-out split and the labelled sets once per freeze hash, and the deck reads only what it stored.

Expected values are hand-counted from the small fixtures below, never computed by the code under test.
"""
import json
from types import SimpleNamespace

import pytest
import yaml

from core.eval import heldout
from core.eval.deck import FIGURES, SLIDES, build
from core.eval.gate4 import HeldOutAlreadyRun, deck_numbers, freeze_hash
from core.eval.heldout import HeldOutIncomplete, compute_results, play, run_heldout
from core.eval.matrix import config_hash

ROOT = heldout.ROOT
SC = [SimpleNamespace(id="A"), SimpleNamespace(id="B")]
ATTEMPTS = {"A": [True, True, True, True], "B": [True, False, True, True]}
LABELLED = {
    "policy_probes": [
        {"id": "p1", "action": "approve_kyc", "forbidden": True, "expected": "BLOCK", "engine": "BLOCK"},
        {"id": "p2", "action": "request_otp", "forbidden": True, "expected": "BLOCK", "engine": "BLOCK"},
        {"id": "p3", "action": "raise_ticket", "forbidden": False, "expected": "ALLOW", "engine": "ALLOW"},
        {"id": "p4", "action": "apply_fee_waiver", "forbidden": False, "expected": "ESCALATE", "engine": "ALLOW"},   # a miss: recall 2/3
    ],
    "escalation_snapshots": [
        {"id": "e1", "kind": "hard", "should_escalate": True, "escalated": True},
        {"id": "e2", "kind": "hard", "should_escalate": True, "escalated": False},
        {"id": "e3", "kind": "soft", "should_escalate": False, "escalated": False},
    ],
    "claims_en": [
        {"id": "c1", "label": "true", "kind": "kb", "sent": True, "sources_tried": 1},
        {"id": "c2", "label": "true", "kind": "db", "sent": False, "sources_tried": 18},
        {"id": "c3", "label": "false", "kind": "db", "sent": False, "sources_tried": 18},
        {"id": "c4", "label": "unsupported", "kind": "none", "sent": True, "sources_tried": 73},
        {"id": "c5", "label": "unsupported", "kind": "none", "sent": False, "sources_tried": 73},
    ],
}


def kn(m):
    return (m["k"], m["n"])


def test_metrics_are_counted_from_the_recorded_outcomes():
    r = compute_results(SC, ATTEMPTS, LABELLED, "code-1", runs=4)
    m = r["metrics"]
    assert kn(m["resolution_rate"]) == (7, 8)
    assert kn(m["forbidden_executed"]) == (0, 2)
    assert kn(m["policy_recall"]) == (2, 3) and kn(m["policy_precision"]) == (2, 2)
    assert kn(m["escalation_recall"]) == (1, 2) and kn(m["escalation_precision"]) == (1, 1)
    assert kn(m["unsupported_claim_rate"]) == (1, 3)          # c4 got through, c3 and c5 did not
    assert kn(m["grounding_false_block"]) == (1, 2)           # c2 was blocked
    assert m["pass_k"]["value"] == 0.5 and m["pass_k"]["all_k_passed"]["k"] == 1   # A: C(4,4)/C(4,4)=1, B: C(3,4)=0
    assert r["failed_scenarios"] == ["B"] and r["code_hash"] == "code-1" and r["scenarios"] == ATTEMPTS
    assert set(r["definitions"]) >= set(m) and r["not_measured"]


def test_a_scenario_that_did_not_run_every_time_stops_the_run():
    with pytest.raises(HeldOutIncomplete, match="B"):
        compute_results(SC, {"A": [True] * 4, "B": [True] * 3}, LABELLED, "c", runs=4)
    with pytest.raises(HeldOutIncomplete, match="B"):
        compute_results(SC, {"A": [True] * 4}, LABELLED, "c", runs=4)


@pytest.mark.parametrize("section", heldout.LABELLED_SETS)
def test_an_empty_labelled_set_stops_the_run(section):
    with pytest.raises(HeldOutIncomplete, match=section):
        compute_results(SC, ATTEMPTS, {**LABELLED, section: []}, "c", runs=4)


def test_fewer_runs_than_k_store_no_pass_k():
    r = compute_results(SC, {"A": [True], "B": [False]}, LABELLED, "c", runs=1)
    assert "pass_k" not in r["metrics"]


def results_for_now():
    return compute_results(SC, ATTEMPTS, LABELLED, config_hash(ROOT), runs=4)


def test_held_out_runs_once_and_a_second_attempt_never_plays(conn, clock):
    out = run_heldout(conn, clock, ROOT, results_for_now)
    assert out["config_hash"] == freeze_hash(ROOT) and out["results"]["metrics"]["resolution_rate"]["k"] == 7
    played = []
    with pytest.raises(HeldOutAlreadyRun):
        run_heldout(conn, clock, ROOT, lambda: played.append(1) or results_for_now())
    assert played == []
    assert conn.execute("SELECT COUNT(*) FROM metrics_runs WHERE split = 'heldout'").fetchone()[0] == 1


def test_a_configuration_change_during_the_run_stores_nothing(conn, clock, monkeypatch):
    hashes = iter(["before", "after"])
    monkeypatch.setattr(heldout, "freeze_hash", lambda root, frozen_root=None: next(hashes))
    with pytest.raises(HeldOutIncomplete, match="changed while"):
        run_heldout(conn, clock, ROOT, results_for_now)
    assert conn.execute("SELECT COUNT(*) FROM metrics_runs").fetchone()[0] == 0


def test_deck_metrics_all_resolve_from_the_stored_run(conn, clock):
    run_heldout(conn, clock, ROOT, results_for_now)
    run = deck_numbers(conn, freeze_hash(ROOT))
    deck = build(yaml.safe_load(SLIDES.read_text(encoding="utf-8")), json.loads(FIGURES.read_text(encoding="utf-8")), run=run,
                 current_hash=freeze_hash(ROOT), draft=True)
    metric_problems = [p for p in deck["problems"] if "metric" in p]
    assert metric_problems == []
    traced = {t["token"]: t for t in deck["trace"] if t["token"].startswith("metric:")}
    assert traced["metric:resolution_rate"]["rendered"].startswith("7 of 8")
    assert traced["metric:forbidden_executed"]["rendered"].startswith("0 of 2")
    assert all(run["run_id"] in t["source"] for t in traced.values())


class FakePytest:
    """Stands in for the pytest subprocesses: writes what the results plugin and tests/heldout_play.py would write."""

    def __init__(self, code, scenario_ids, fail_play=False):
        self.code, self.ids, self.fail_play, self.calls = code, scenario_ids, fail_play, []

    def __call__(self, args, env):
        self.calls.append((args, env))
        if "--scenario-split" in args:
            assert args[args.index("--scenario-split") + 1] == "heldout"
            path = args[args.index("--scenario-results") + 1]
            try:
                store = json.loads(open(path, encoding="utf-8").read())
            except FileNotFoundError:
                store = {"config_hash": self.code, "runs": 0, "last_run": None, "scenarios": {}}
            for sid in self.ids:
                store["scenarios"].setdefault(sid, {"attempts": [], "last_problem": ""})["attempts"].append(True)
            open(path, "w", encoding="utf-8").write(json.dumps(store))
            return 0
        assert args == [heldout.PLAY_FILE]
        if self.fail_play:
            return 1
        open(env["MUNIM_LABELLED_OUT"], "w", encoding="utf-8").write(json.dumps(LABELLED))
        return 0


def test_play_runs_the_heldout_split_k_times_then_the_labelled_sets_once():
    ids = [s.id for s in heldout.heldout_scenarios(ROOT)]
    assert len(ids) >= 54                                     # every pack's held-out scenarios, not only the merchant pack's
    fake = FakePytest(config_hash(ROOT), ids)
    r = play(ROOT, 4, fake)
    assert [("--scenario-split" in a) for a, _ in fake.calls] == [True, True, True, True, False]
    assert kn(r["metrics"]["resolution_rate"]) == (4 * len(ids), 4 * len(ids))


def test_play_refuses_when_the_labelled_sets_fail_or_the_code_moved():
    ids = [s.id for s in heldout.heldout_scenarios(ROOT)]
    with pytest.raises(HeldOutIncomplete, match="labelled sets"):
        play(ROOT, 4, FakePytest(config_hash(ROOT), ids, fail_play=True))
    with pytest.raises(HeldOutIncomplete, match="different code"):
        play(ROOT, 4, FakePytest("some-older-code", ids))


def test_cli_without_confirm_plays_and_stores_nothing(db_path, monkeypatch, capsys):
    monkeypatch.setattr(heldout, "play", lambda *a, **k: pytest.fail("played without --confirm"))
    assert heldout.main(["--db", str(db_path)]) == 0
    assert "Nothing was run" in capsys.readouterr().out


def test_cli_refuses_a_second_run(db_path, clock, monkeypatch, capsys):
    from core.memory.store import connect
    run_heldout(connect(db_path), clock, ROOT, results_for_now)
    monkeypatch.setattr(heldout, "play", lambda *a, **k: pytest.fail("played a second time"))
    assert heldout.main(["--db", str(db_path), "--confirm"]) == 3
    assert "already exists" in capsys.readouterr().err


def test_munim_cli_routes_heldout(db_path, monkeypatch):
    from core import cli
    seen = {}
    monkeypatch.setattr(heldout, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    cli.main(["--db", str(db_path), "heldout", "--runs", "2"])
    assert seen["argv"] == ["--db", str(db_path), "--runs", "2"]
