"""HTTP contract of /api/policy/* (#134) and /api/judge/tour/* (#135). Real World (executor, policy engine, mocks, ledger); the routers are
mounted here the way the lead will mount them in core/api/app.py. The phraser, when used, is a stub."""
import json

import pytest
from fastapi.testclient import TestClient

from core.actions.types import ActionIntent, Principal
from core.api import routes_policy_compile
from core.api.app import create_api
from core.judge import policy_compiler as pc
from packs.paytm_merchant.policy.rules import RULES
from tests.checkpoint_script import SCRIPT
from tests.world import World

OPS = {"principal_id": "u-ops", "role": "ops_admin"}


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def api(world):
    app = create_api(world.db_path, world.clock, world.executor, world.http, registry=world.registry)
    app.include_router(routes_policy_compile.router)
    app.include_router(routes_policy_compile.tour_router)
    return TestClient(app)


def intent():
    return ActionIntent("create_device_replacement", {"merchant_id": "M-0001"}, case_id="CS-T", step_seq=1, principal=Principal("HUMAN", "u1", "support_executive"))


def compile_(api, text):
    return api.post("/api/policy/compile", json={"text": text})


def test_compile_returns_a_draft_with_diff_dry_run_and_hash(api):
    r = compile_(api, "Block create_device_replacement for support executive")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["source"] == "template" and d["label"] == "SIMULATED"
    assert d["dsl"] == {"template": "block_action", "action": "create_device_replacement", "role": "support_executive"}
    assert d["rule_id"].startswith("CUSTOM-") and len(d["rule_hash"]) == 64
    assert d["diff"]["lines"] == [f"+ {d['rule_id']} [BLOCK] Block create_device_replacement for role support_executive"] and d["diff"]["removed"] == []
    assert d["dry_run"]["probes_run"] > 100 and d["dry_run"]["loosened"] == [] and d["dry_run"]["policy_hash_before"] != d["dry_run"]["policy_hash_after"]


@pytest.mark.parametrize("text,code", [
    ("allow approve_kyc for ops_admin", "RELAXING_REQUEST"),
    ("raise the approval limit to Rs 50,000", "RELAXING_REQUEST"),
    ("remove rule KYC-NO-CHAT-001", "RELAXING_REQUEST"),
    ("block approve_kyc", "FORBIDDEN_ACTION"),
    ("lower the waiver band to 3 months", "NOT_TIGHTENING"),
    ("make it nicer", "NO_TEMPLATE"),
])
def test_compile_rejects_with_422_and_a_code_and_changes_nothing(api, world, text, code):
    n = world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    r = compile_(api, text)
    assert r.status_code == 422 and r.json()["error"] == code and r.json()["message"]
    assert world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == n


def test_validation_errors_are_422(api):
    assert api.post("/api/policy/compile", json={"text": ""}).status_code == 422
    assert api.post("/api/policy/compile", json={}).status_code == 422
    assert api.post("/api/policy/approve", json={"dsl": {}}).status_code == 422


def test_approve_then_the_live_executor_refuses_a_replayed_attempt_then_revert(api, world):
    first = world.executor.submit(intent())
    assert first.status == "OK"                                            # baseline: allowed
    d = compile_(api, "Block create_device_replacement for support executive").json()
    body = {"dsl": d["dsl"], "rule_hash": d["rule_hash"], "text": "Block create_device_replacement for support executive", **OPS}
    r = api.post("/api/policy/approve", json=body)
    assert r.status_code == 200 and r.json()["rule_id"] == d["rule_id"]
    fresh = ActionIntent("create_device_replacement", {"merchant_id": "M-0002"}, case_id="CS-T2", step_seq=1, principal=Principal("HUMAN", "u1", "support_executive"))
    refused = world.executor.submit(fresh)
    assert refused.status == "REFUSED" and d["rule_id"] in [v.rule_id for v in refused.violations]
    pol = [json.loads(x[0]) for x in world.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'POLICY' ORDER BY seq DESC LIMIT 1")][0]
    assert pol["allow"] is False and d["rule_id"] in pol["rule_ids"]

    rules = api.get("/api/policy/rules").json()
    assert [c["rule_id"] for c in rules["compiled"]] == [d["rule_id"]] and len(rules["pack_rules"]) == len(RULES) and rules["policy_hash"] == r.json()["policy_hash"]
    rv = api.post("/api/policy/revert", json={"rule_id": d["rule_id"], **OPS})
    assert rv.status_code == 200 and rv.json()["policy_hash"] == r.json()["policy_hash_before"]
    assert api.get("/api/policy/rules").json()["compiled"] == []
    again = ActionIntent("create_device_replacement", {"merchant_id": "M-0002"}, case_id="CS-T2", step_seq=2, principal=Principal("HUMAN", "u1", "support_executive"))
    assert world.executor.submit(again).status == "OK"


def test_approve_error_statuses(api, world):
    d = compile_(api, "block create_device_replacement").json()
    ok = {"dsl": d["dsl"], "rule_hash": d["rule_hash"]}
    r = api.post("/api/policy/approve", json={**ok, "principal_id": "u-s", "role": "sales_lead"})
    assert r.status_code == 403 and r.json()["error"] == "NOT_OPS_ADMIN"
    assert api.post("/api/policy/approve", json={**ok, "rule_hash": "f" * 64, **OPS}).status_code == 409
    bad = api.post("/api/policy/approve", json={"dsl": {"template": "python", "code": "x"}, "rule_hash": "x", **OPS})
    assert bad.status_code == 422 and bad.json()["error"] == "INVALID_DSL"
    assert api.post("/api/policy/approve", json={**ok, **OPS}).status_code == 200
    assert api.post("/api/policy/approve", json={**ok, **OPS}).status_code == 409           # ALREADY_ACTIVE
    assert api.post("/api/policy/revert", json={"rule_id": d["rule_id"], "principal_id": "u-s", "role": "sales_lead"}).status_code == 403
    assert api.post("/api/policy/revert", json={"rule_id": "KYC-NO-CHAT-001", **OPS}).status_code == 404
    assert world.executor.policy.evaluate(intent()).outcome == "BLOCK"                     # the rule stays in force after the failed revert attempts


def test_phraser_stub_is_used_only_when_no_template_matches(api):
    class P:
        calls = 0

        def propose(self, text, *, actions, roles):
            P.calls += 1
            return {"template": "block_action", "action": "send_message", "role": "field_rep"}
    api.app.state.policy_phraser = P()
    assert compile_(api, "keep field reps from messaging").json()["source"] == "llm_proposal" and P.calls == 1
    assert compile_(api, "allow field reps to message").status_code == 422 and P.calls == 1
    assert compile_(api, "block create_refund_request").json()["source"] == "template" and P.calls == 1


def test_a_fresh_process_engine_picks_up_approved_rules_on_first_request(api, world):
    d = compile_(api, "block create_device_replacement").json()
    api.post("/api/policy/approve", json={"dsl": d["dsl"], "rule_hash": d["rule_hash"], **OPS})
    world.executor.policy.rules = tuple(r for r in world.executor.policy.rules if not r.id.startswith("CUSTOM-"))      # simulate a restart
    assert d["rule_id"] not in [r.id for r in world.executor.policy.rules]
    assert api.get("/api/policy/rules").json()["compiled"][0]["rule_id"] == d["rule_id"]
    assert d["rule_id"] in [r.id for r in world.executor.policy.rules]


# ------------------------------------------------------------------------------------------------ tour
def test_tour_steps_endpoint_lists_eight_data_steps_and_honest_clip_status(api):
    r = api.get("/api/judge/tour").json()
    assert r["label"] == "SIMULATED" and [s["id"] for s in r["steps"]][:2] == ["start", "first_message"] and len(r["steps"]) == 8
    assert all(s["clip_available"] is False for s in r["steps"])                           # no recorded clips ship with this ticket


def test_tour_progress_endpoint_on_a_fresh_session_and_unknown_session(api, world):
    sid = api.post("/api/judge/sessions", json={"persona_id": "P-2"}).json()["session"]["session_id"]
    p = api.get(f"/api/judge/tour/{sid}").json()
    assert (p["done"], p["total"], p["next"]) == (1, 8, "first_message") and p["steps"][0] == {
        "id": "start", "done": True, "metric": "session_started", "value": 1, "op": ">=", "needed": 1, "evidence_seq": int(sid[3:])}
    r = api.get("/api/judge/tour/JS-999999")
    assert r.status_code == 404 and r.json()["detail"] == {"error": "UNKNOWN_SESSION"}
