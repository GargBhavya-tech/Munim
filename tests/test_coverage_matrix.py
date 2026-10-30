"""#69: the coverage matrix is computed from recorded test results only, and 'Kyun nahi?' explains refusals from rule metadata.

Each 'watch out' has a test that fails if the mistake is present: hand-marked support, stale results shown as current,
a latest pass hiding an earlier held-out failure, and counting fewer than four held-out attempts as pass^4.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.actions.types import ActionIntent, Principal
from core.api.app import create_api
from core.eval.matrix import (PASS_K, compute_matrix, config_hash, empty_store, explain_refusal, load_store, record_outcomes)
from core.eval.scenario import Scenario, load_scenarios
from packs.paytm_merchant.policy.rules import default_rules
from tests.checkpoint_script import SCRIPT
from tests.world import World

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "packs" / "paytm_merchant"
H = "hash-now"
HUMAN = Principal("HUMAN", "u1", "support_executive")


def sc(pid, name, split, family="happy"):
    return Scenario.model_validate({
        "id": f"{pid}-{split}-{name}", "playbook_id": pid, "family": family, "description": "t", "split": split,
        "persona": {"merchant_id": "M-0001", "language": "en-IN"}, "steps": [{"merchant_says": "hi"}],
        "expected": {"case_state": "RESOLVED", "escalation": {"expected": False}}})


def library(pid="P1"):
    return ([sc(pid, f"d{i}", "dev") for i in range(5)] + [sc(pid, "adv", "dev", "adversarial")]
            + [sc(pid, f"h{i}", "heldout") for i in range(2)] + [sc(pid, "hesc", "heldout", "should_escalate")])


def record(scs, attempts, chash=H, fail=()):
    store = empty_store(chash)
    for n in range(attempts):
        store = record_outcomes(store, chash, {s.id: (s.id not in fail, "boom") for s in scs}, f"2026-01-0{n + 1}T00:00:00")
    return store


def row(scs, store, chash=H):
    return compute_matrix(scs, store, chash)["playbooks"][0]


# ---- computed from results ----------------------------------------------------------------------------------------
def test_supported_only_when_every_scenario_passed_and_heldout_has_four_clean_attempts():
    scs = library()
    r = row(scs, record(scs, PASS_K))
    assert r["supported"] is True and r["why_not"] == []
    assert r["heldout"] == {"n": 3, "passed": 3, "attempts": 4, "pass_k": 4, "pass_k_ok": True}
    assert r["dev"] == {"n": 6, "passed": 6} and r["safety"] == {"n": 2, "passed": 2}


def test_no_results_means_unsupported_even_though_the_bar_is_met():
    scs = library()
    r = row(scs, empty_store(H))
    assert not r["supported"] and any("not run" in w for w in r["why_not"])


def test_three_heldout_attempts_is_not_pass_4():
    scs = library()
    r = row(scs, record(scs, 3))
    assert not r["supported"] and "held-out pass^4 needs 4 recorded attempts, have 3" in r["why_not"]


def test_an_earlier_heldout_failure_is_not_hidden_by_a_later_pass():
    scs = library()
    store = record(scs, 3, fail={"P1-heldout-h0"})
    store = record_outcomes(store, H, {s.id: (True, "") for s in scs}, "2026-01-09T00:00:00")     # 4th attempt passes
    r = row(scs, store)
    assert not r["supported"] and any("failed an attempt: P1-heldout-h0" in w for w in r["why_not"])
    assert r["heldout"]["pass_k_ok"] is False


def test_a_failing_dev_or_safety_scenario_blocks_support_and_is_named():
    scs = library()
    r = row(scs, record(scs, 4, fail={"P1-dev-adv"}))
    assert not r["supported"]
    assert any("dev scenarios failing: P1-dev-adv" in w for w in r["why_not"])
    assert any("safety scenarios failing: P1-dev-adv" in w for w in r["why_not"])


def test_stale_results_never_show_as_supported():
    scs = library()
    m = compute_matrix(scs, record(scs, 4, chash="older-hash"), H)
    assert m["stale"] is True and m["supported_count"] == 0
    assert "results are stale" in m["playbooks"][0]["why_not"][0]


def test_below_the_scenario_bar_is_unsupported_even_if_everything_passes():
    scs = library()[:4]
    r = row(scs, record(scs, 4))
    assert not r["supported"] and any("need 6" in w for w in r["why_not"]) and any("need 3" in w for w in r["why_not"])


def test_there_is_no_way_to_mark_a_playbook_supported_by_hand():
    scs = library()
    store = record(scs, 1)
    store["supported"] = True
    store["playbooks"] = {"P1": {"supported": True}}
    assert not row(scs, store)["supported"]               # keys that are not recorded outcomes are ignored


def test_a_playbook_with_no_scenarios_still_gets_a_row():
    m = compute_matrix(library(), empty_store(H), H, {"P2": "service"}, playbook_ids=["P1", "P2"])
    p2 = next(r for r in m["playbooks"] if r["playbook_id"] == "P2")
    assert p2["supported"] is False and p2["half"] == "service" and p2["dev"]["n"] == 0


# ---- the results store ----------------------------------------------------------------------------------------------
def test_attempts_accumulate_and_a_changed_hash_discards_them():
    s = record_outcomes(empty_store("a"), "a", {"X": (True, "")}, "t1")
    s = record_outcomes(s, "a", {"X": (False, "bad")}, "t2")
    assert s["scenarios"]["X"] == {"attempts": [True, False], "last_problem": "bad"} and s["runs"] == 2
    s = record_outcomes(s, "b", {"X": (True, "")}, "t3")
    assert s["scenarios"]["X"]["attempts"] == [True] and s["config_hash"] == "b" and s["runs"] == 1


def test_a_missing_or_corrupt_results_file_is_an_empty_store(tmp_path):
    assert load_store(tmp_path / "nope.json") == empty_store()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_store(bad) == empty_store()


def test_config_hash_changes_with_a_playbook_and_ignores_line_endings(tmp_path):
    for d in ("core", "packs/p/playbooks", "config"):
        (tmp_path / d).mkdir(parents=True)
    pb = tmp_path / "packs/p/playbooks/a.yaml"
    pb.write_bytes(b"id: A\nsteps: []\n")
    h1 = config_hash(tmp_path)
    pb.write_bytes(b"id: A\r\nsteps: []\r\n")
    assert config_hash(tmp_path) == h1
    pb.write_bytes(b"id: A\nsteps: [x]\n")
    assert config_hash(tmp_path) != h1


# ---- the real library ----------------------------------------------------------------------------------------------
def test_real_library_with_no_results_has_no_supported_playbook_and_includes_c2():
    scs = load_scenarios(PACK / "scenarios")
    pids = [p.stem for p in (PACK / "playbooks").glob("*.yaml")]
    m = compute_matrix(scs, empty_store(), config_hash(ROOT), playbook_ids=pids)
    assert m["supported_count"] == 0 and len(m["playbooks"]) >= len(pids)
    assert "C2_SOUNDBOX_FAULT" in {r["playbook_id"] for r in m["playbooks"]}


# ---- Kyun nahi? -----------------------------------------------------------------------------------------------------
def test_explain_uses_rule_metadata_and_the_recorded_reason():
    payload = {"action": "clear_risk_hold", "allow": False, "rule_ids": ["ROLE-001", "RISK-HOLD-001"], "outcome": "BLOCK",
               "reasons": ["support_executive may not clear a hold", "a hold goes to the risk reviewer"]}
    e = explain_refusal(payload, default_rules())
    assert [x["rule_id"] for x in e["rules"]] == ["ROLE-001", "RISK-HOLD-001"]
    rule = next(r for r in default_rules() if r.id == "ROLE-001")
    assert e["rules"][0]["title"] == rule.title and e["rules"][0]["source"] == rule.source
    assert e["rules"][1]["reason"] == "a hold goes to the risk reviewer" and e["allowed"] is False


def test_explain_covers_registry_kill_switch_and_unknown_ids():
    e = explain_refusal({"action": "approve_kyc", "allow": False, "rule_ids": ["REGISTRY"], "reason": "not in the registry"}, [])
    assert e["rules"][0]["title"] == "Unknown or forbidden action" and e["rules"][0]["reason"] == "not in the registry"
    e = explain_refusal({"action": "x", "allow": False, "rule_ids": ["KILL_SWITCH"], "reason": "halted"}, [])
    assert e["rules"][0]["title"] == "Kill switch is on"
    for rid in ("NOT-ALLOWLISTED", "ARG-FROM-MERCHANT", "WRONG-SUBJECT", "MODEL-WRITTEN-TEXT", "INVALID-ARGS", "UNKNOWN-ACTION", "PLAYBOOK-LIMIT", "CONTACT-BUDGET"):
        assert explain_refusal({"action": "x", "allow": False, "rule_ids": [rid], "reasons": ["why"]}, [])["rules"][0]["title"] != "Unknown rule", rid
    e = explain_refusal({"action": "x", "allow": False, "rule_ids": ["MADE-UP-9"]}, [])
    assert e["rules"][0]["title"] == "Unknown rule" and e["rules"][0]["reason"] is None


# ---- API ------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


def make_api(world, tmp_path, results=None):
    path = tmp_path / "results.json"
    if results is not None:
        path.write_text(json.dumps(results))
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http, results_path=path))


def test_coverage_endpoint_is_stale_until_results_for_this_code_exist(world, tmp_path):
    body = make_api(world, tmp_path).get("/api/coverage").json()
    assert body["stale"] is True and body["supported_count"] == 0 and body["results_config_hash"] is None
    assert {"playbook_id", "supported", "why_not", "dev", "heldout", "safety", "families"} <= set(body["playbooks"][0])
    scs = load_scenarios(PACK / "scenarios")
    store = empty_store(config_hash(ROOT))
    for n in range(PASS_K):
        store = record_outcomes(store, config_hash(ROOT), {s.id: (True, "") for s in scs}, f"2026-01-0{n + 1}T00:00:00")
    body = make_api(world, tmp_path, store).get("/api/coverage").json()
    assert body["stale"] is False
    sup = {r["playbook_id"] for r in body["playbooks"] if r["supported"]}
    assert {"S2_SOUNDBOX_UPSELL", "S1_ACTIVATION", "C2_SOUNDBOX_FAULT"} <= sup                 # S1 and C2 reached the bar in #70 and #79


def test_why_endpoint_explains_a_refusal_and_rejects_non_refusals(world, tmp_path):
    api = make_api(world, tmp_path)
    refused = world.executor.submit(ActionIntent("approve_kyc", {"merchant_id": "M-0006"}, case_id="CS-W", step_seq=1, principal=HUMAN, channel="app"))
    assert refused.status == "REFUSED"
    seq = world.conn.execute("SELECT seq FROM ledger WHERE kind='POLICY' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    j = api.get(f"/api/why/{seq}").json()
    assert j["allowed"] is False and j["action"] == "approve_kyc" and j["rules"][0]["rule_id"] == "REGISTRY"
    assert api.get("/api/why/999999").status_code == 404
    ok = world.executor.submit(ActionIntent("send_kyc_resume_link", {"merchant_id": "M-0006"}, case_id="CS-W", step_seq=2, principal=HUMAN, channel="app"))
    assert ok.status == "OK"
    allowed = world.conn.execute("SELECT seq FROM ledger WHERE kind='POLICY' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    assert api.get(f"/api/why/{allowed}").status_code == 409
