"""#148 rollout ladder: per-playbook levels, promotion only when every floor holds, automatic demotion, every change ledgered."""
import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.ledger import Ledger
from core.eval.ladder import MIN_CASES, SOLVED_FLOOR, Baseline, Evidence, Ladder, floor_failures
from tests.checkpoint_script import SCRIPT
from tests.world import World

BASE = Baseline(reopen_rate=0.10, repeat_contact_rate=0.20, hours_to_resolution=24.0, first_contact_resolution=0.60)
GOOD = Evidence(cases=MIN_CASES, forbidden_executed=0, reopen_rate=0.08, repeat_contact_rate=0.15, hours_to_resolution=20.0, first_contact_resolution=0.70, solved_rate=0.90)


def worse(**kw):
    return Evidence(**{**GOOD.__dict__, **kw})


@pytest.fixture
def ladder(conn, clock):
    return Ladder(conn, clock, Ledger(conn, clock))


def test_every_playbook_starts_at_l0_and_is_promoted_one_level_at_a_time(ladder):
    assert ladder.level("C1_SETTLEMENT_DELAY") == "L0"
    d = ladder.step("C1_SETTLEMENT_DELAY", GOOD, BASE)
    assert (d.action, d.level_before, d.level_after) == ("PROMOTE", "L0", "L1") and ladder.level("C1_SETTLEMENT_DELAY") == "L1"
    assert ladder.step("C1_SETTLEMENT_DELAY", GOOD, BASE).level_after == "L2"
    top = ladder.step("C1_SETTLEMENT_DELAY", GOOD, BASE)
    assert top.action == "HOLD" and top.level_after == "L2" and ladder.level("C1_SETTLEMENT_DELAY") == "L2"          # there is no L3


@pytest.mark.parametrize("kw,fragment", [
    ({"reopen_rate": 0.11}, "reopen rate"), ({"repeat_contact_rate": 0.21}, "repeat-contact"), ({"hours_to_resolution": 24.5}, "time to resolution"),
    ({"first_contact_resolution": 0.59}, "first-contact"), ({"solved_rate": SOLVED_FLOOR - 0.01}, "'solved?'"),
])
def test_one_broken_floor_blocks_promotion_and_names_the_floor(ladder, kw, fragment):
    d = ladder.evaluate("C3_PAYMENT_FAILED", worse(**kw), BASE)
    assert d.action == "HOLD" and d.level_after == "L0" and any(fragment in r for r in d.reasons)
    assert ladder.step("C3_PAYMENT_FAILED", worse(**kw), BASE).level_after == "L0"


def test_the_floors_are_inclusive_so_matching_the_baseline_exactly_passes():
    at = Evidence(cases=MIN_CASES, reopen_rate=0.10, repeat_contact_rate=0.20, hours_to_resolution=24.0, first_contact_resolution=0.60, solved_rate=SOLVED_FLOOR)
    assert floor_failures(at, BASE) == []


def test_too_few_cases_never_promote(ladder):
    d = ladder.step("C4_MERCHANT_REFUND", worse(cases=MIN_CASES - 1), BASE)
    assert d.action == "HOLD" and ladder.level("C4_MERCHANT_REFUND") == "L0" and "only 29 cases" in d.reasons[0]


def test_one_forbidden_action_sends_that_playbook_to_l0_and_touches_no_other(ladder, conn):
    for pb in ("C1_SETTLEMENT_DELAY", "C2_SOUNDBOX_FAULT", "S2_SOUNDBOX_UPSELL"):
        ladder.step(pb, GOOD, BASE)
        ladder.step(pb, GOOD, BASE)
        assert ladder.level(pb) == "L2"
    d = ladder.step("C2_SOUNDBOX_FAULT", worse(forbidden_executed=1), BASE)
    assert d.action == "DEMOTE" and (d.level_before, d.level_after) == ("L2", "L0") and "forbidden" in d.reasons[0]
    assert ladder.level("C2_SOUNDBOX_FAULT") == "L0" and ladder.level("C1_SETTLEMENT_DELAY") == "L2" and ladder.level("S2_SOUNDBOX_UPSELL") == "L2"


def test_any_other_broken_floor_demotes_one_level_with_no_waiting(ladder):
    for _ in range(2):
        ladder.step("C5_DETAIL_UPDATE", GOOD, BASE)
    d = ladder.step("C5_DETAIL_UPDATE", worse(cases=3, repeat_contact_rate=0.5), BASE)          # even with few cases, a regression demotes immediately
    assert d.action == "DEMOTE" and d.level_after == "L1" and ladder.level("C5_DETAIL_UPDATE") == "L1"


def test_a_playbook_already_at_l0_cannot_be_demoted_further(ladder):
    d = ladder.step("C6_CHARGE_DISPUTE", worse(forbidden_executed=2), BASE)
    assert d.action == "HOLD" and d.level_after == "L0"


def test_every_move_is_ledgered_and_a_hold_is_not(ladder, conn, clock):
    ladder.step("C7_RETURN_CLOSURE", GOOD, BASE)
    ladder.step("C7_RETURN_CLOSURE", worse(cases=1), BASE)                                       # a hold
    ladder.step("C7_RETURN_CLOSURE", worse(forbidden_executed=1), BASE)
    moves = [e.payload for e in Ledger(conn, clock).entries() if e.kind == "STATE" and "rollout" in e.payload]
    assert [(m["rollout"], m["from"], m["to"]) for m in moves] == [("PROMOTE", "L0", "L1"), ("DEMOTE", "L1", "L0")]
    assert all(m["playbook"] == "C7_RETURN_CLOSURE" and m["reasons"] for m in moves)


def test_the_api_lists_every_playbook_with_its_level(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    lad = Ladder(w.conn, w.clock, w.ledger)
    lad.step("C1_SETTLEMENT_DELAY", GOOD, BASE)
    api = TestClient(create_api(w.db_path, w.clock, w.executor, w.http))
    body = api.get("/api/ladder").json()
    levels = {p["playbook_id"]: p["level"] for p in body["playbooks"]}
    assert body["levels"] == {"L0": "shadow", "L1": "assisted", "L2": "autonomous"} and len(levels) >= 15
    assert levels["C1_SETTLEMENT_DELAY"] == "L1" and levels["C2_SOUNDBOX_FAULT"] == "L0" and body["label"] == "SIMULATED"


def test_a_real_database_error_is_raised_not_shown_as_every_playbook_at_l0(db_path, clock, monkeypatch):
    import sqlite3
    from core.api import routes_ladder
    w = World(db_path, clock, SCRIPT)

    class Broken:
        def execute(self, *a):
            raise sqlite3.OperationalError("database disk image is malformed")

        def close(self):
            pass
    monkeypatch.setattr(routes_ladder, "open_ro", lambda path: Broken())
    with pytest.raises(sqlite3.OperationalError):
        TestClient(create_api(w.db_path, w.clock, w.executor, w.http)).get("/api/ladder")
