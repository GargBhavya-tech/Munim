"""Gate 4 machinery (#146): the held-out set runs once per configuration hash; deck numbers come only from that run."""
import shutil
from pathlib import Path

import pytest

from core.eval.gate4 import HeldOutAlreadyRun, HeldOutRunner, NoFrozenRun, deck_numbers, freeze_hash, matrix_store_from_run
from core.eval.metrics import FROZEN_ROOT, FrozenSetTampered

from tests.checkpoint_script import SCRIPT  # noqa: E402
from tests.test_coverage_matrix import make_api  # noqa: E402
from tests.world import World  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def frozen(tmp_path):
    dst = tmp_path / "frozen"
    shutil.copytree(FROZEN_ROOT, dst)
    return dst


def test_hash_is_stable(frozen):
    h1 = freeze_hash(ROOT, frozen)
    assert h1 == freeze_hash(ROOT, frozen)
    assert len(h1) == 20


def test_a_tampered_frozen_set_stops_the_hash(frozen):
    (frozen / "probes" / "policy_probes.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FrozenSetTampered):
        freeze_hash(ROOT, frozen)


def test_heldout_runs_once_per_hash(conn, clock):
    r = HeldOutRunner(conn, clock)
    assert r.run("heldout", "h1", lambda: {"leaks": 0}) == {"leaks": 0}
    with pytest.raises(HeldOutAlreadyRun):
        r.run("heldout", "h1", lambda: {"leaks": 0})
    assert conn.execute("SELECT COUNT(*) FROM metrics_runs WHERE split='heldout'").fetchone()[0] == 1
    r.run("heldout", "h2", lambda: {"leaks": 1})                  # a changed configuration is a new hash and may run
    assert conn.execute("SELECT COUNT(*) FROM metrics_runs WHERE split='heldout'").fetchone()[0] == 2


def test_refused_second_run_does_not_call_play(conn, clock):
    r = HeldOutRunner(conn, clock)
    r.run("heldout", "h1", lambda: {})
    called = []
    with pytest.raises(HeldOutAlreadyRun):
        r.run("heldout", "h1", lambda: called.append(1) or {})
    assert called == []


def test_dev_runs_are_unlimited_and_do_not_count_as_heldout(conn, clock):
    r = HeldOutRunner(conn, clock)
    for _ in range(3):
        r.run("dev", "h1", lambda: {"x": 1})
    assert r.heldout_for("h1") is None
    r.run("heldout", "h1", lambda: {"x": 2})
    assert r.heldout_for("h1")["results"] == {"x": 2}
    with pytest.raises(ValueError):
        r.run("test", "h1", lambda: {})


def test_deck_numbers_refuse_without_a_run_or_after_a_change(conn, clock):
    with pytest.raises(NoFrozenRun, match="no held-out run"):
        deck_numbers(conn, "h1")
    HeldOutRunner(conn, clock).run("heldout", "h1", lambda: {"leaks": 0})
    assert deck_numbers(conn, "h1")["results"] == {"leaks": 0}
    with pytest.raises(NoFrozenRun, match="changed after it"):
        deck_numbers(conn, "h2")


def test_dev_run_never_feeds_the_deck(conn, clock):
    HeldOutRunner(conn, clock).run("dev", "h1", lambda: {"leaks": 0})
    with pytest.raises(NoFrozenRun):
        deck_numbers(conn, "h1")


def test_matrix_store_shows_hash_and_time(conn, clock):
    res = {"code_hash": "abc", "scenarios": {"S-01": [True, True], "S-02": [True, False]}}
    HeldOutRunner(conn, clock).run("heldout", "h1", lambda: res)
    store = matrix_store_from_run(deck_numbers(conn, "h1"))
    assert store["config_hash"] == "abc"
    assert store["runs"] == 2
    assert store["scenarios"]["S-02"]["attempts"] == [True, False]
    assert store["last_run"]["config_hash"] == "h1" and store["last_run"]["recorded_at"]


def test_coverage_route_serves_heldout_run_and_marks_it_stale(db_path, clock, tmp_path):
    world = World(db_path, clock, SCRIPT)
    http = make_api(world, tmp_path)
    assert http.get("/api/coverage?source=heldout").status_code == 404
    assert http.get("/api/coverage?source=nope").status_code == 400
    HeldOutRunner(world.conn, world.clock).run("heldout", "h1", lambda: {"code_hash": "old-code", "scenarios": {}})
    world.conn.commit()
    body = http.get("/api/coverage?source=heldout").json()
    assert body["results_config_hash"] == "old-code" and body["stale"] is True
    assert body["last_run"]["config_hash"] == "h1" and body["last_run"]["recorded_at"]
    assert all(r["supported"] is False and r["why_not"] for r in body["playbooks"])


# ---- A8: the database itself holds the once-only rule, and the freeze hash covers the model weights ---------------------------
def test_the_database_itself_refuses_a_second_held_out_row_for_a_hash(conn, clock):
    import sqlite3
    HeldOutRunner(conn, clock)                                                     # makes sure the unique index exists on older databases
    conn.execute("INSERT INTO metrics_runs(run_id, config_hash, split, started_at, results_json) VALUES ('MR-A', 'h1', 'heldout', 'x', '{}')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO metrics_runs(run_id, config_hash, split, started_at, results_json) VALUES ('MR-B', 'h1', 'heldout', 'x', '{}')")
    conn.execute("INSERT INTO metrics_runs(run_id, config_hash, split, started_at, results_json) VALUES ('MR-C', 'h1', 'dev', 'x', '{}')")
    conn.execute("INSERT INTO metrics_runs(run_id, config_hash, split, started_at, results_json) VALUES ('MR-D', 'h1', 'dev', 'x', '{}')")


def test_a_race_past_the_python_check_is_still_refused(conn, clock, monkeypatch):
    r = HeldOutRunner(conn, clock)
    r.run("heldout", "h1", lambda: {})
    monkeypatch.setattr(r, "_runs", lambda split, h: [])                          # pretend the check-then-insert raced
    with pytest.raises(HeldOutAlreadyRun):
        r.run("heldout", "h1", lambda: {})


def fake_hf(tmp_path, revisions: dict):
    for name, rev in revisions.items():
        d = tmp_path / "hub" / f"models--{name.replace('/', '--')}" / "refs"
        d.mkdir(parents=True)
        (d / "main").write_text(rev, encoding="utf-8")
    return tmp_path / "hub"


def test_the_freeze_hash_changes_when_model_weights_change_under_the_same_name(frozen, tmp_path):
    from core.eval.gate4 import MODELS, model_revisions
    a = fake_hf(tmp_path / "a", {m: "rev-1" for m in MODELS})
    b = fake_hf(tmp_path / "b", {**{m: "rev-1" for m in MODELS}, MODELS[0]: "rev-2"})
    assert model_revisions(a) == {m: "rev-1" for m in MODELS}
    assert freeze_hash(ROOT, frozen, hf_cache=a) == freeze_hash(ROOT, frozen, hf_cache=a)
    assert freeze_hash(ROOT, frozen, hf_cache=a) != freeze_hash(ROOT, frozen, hf_cache=b)
    assert model_revisions(tmp_path / "empty") == {m: "absent" for m in MODELS}     # no weights downloaded: recorded as absent, never guessed
