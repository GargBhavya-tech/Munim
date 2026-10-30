"""Cases, case detail, cost, approvals list, health, undo - against the seeded database with a REAL executor."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.ledger import Ledger
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def api(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http, registry=world.registry))


def run_s1_to_device(world):
    world.inject("E-STALL", "SIGNUP_STALLED")
    world.dispatcher.run()
    world.clock.advance(timedelta(hours=2))
    world.control(f"/mock/control/kyc/{world.mid}", status="APPROVED")
    world.inject("E-KYC", "KYC_SUBMITTED")
    world.dispatcher.run()


def test_health_reports_sim_time(api, world):
    assert api.get("/api/health").json() == {"ok": True, "sim_time": "2026-10-31T09:00:00+05:30"}


def test_cases_list_and_filters(api, world):
    assert api.get("/api/cases").json() == {"cases": []}
    run_s1_to_device(world)
    cases = api.get("/api/cases").json()["cases"]
    assert len(cases) == 1
    c = cases[0]
    assert set(c) == {"case_id", "merchant_id", "lead_id", "playbook_id", "state", "lane", "language", "step", "touches", "opened_at", "closed_at", "resume_note"}
    assert (c["case_id"], c["merchant_id"], c["playbook_id"], c["state"], c["step"]) == ("CS-0001", "M-0006", "S1_ACTIVATION", "WAITING", "device")
    assert api.get("/api/cases?state=WAITING").json()["cases"] == cases
    assert api.get("/api/cases?state=RESOLVED").json()["cases"] == []
    assert api.get("/api/cases?merchant_id=M-0002").json()["cases"] == []
    assert api.get(f"/api/cases?lane={c['lane']}&merchant_id=M-0006").json()["cases"] == cases


def test_case_detail_shapes(api, world):
    run_s1_to_device(world)
    d = api.get("/api/cases/CS-0001").json()
    assert set(d) == {"case", "timeline", "actions", "claims", "messages", "renders", "receipts"}
    assert d["case"]["case_id"] == "CS-0001"
    tl = d["timeline"]
    assert [e["seq"] for e in tl] == sorted(e["seq"] for e in tl) and len(tl) > 5
    assert all(e["case_id"] == "CS-0001" for e in tl)
    assert set(tl[0]) == {"seq", "case_id", "ts_sim", "actor", "role", "kind", "payload", "policy_hash", "model_id", "hash"}
    names = [a["name"] for a in d["actions"]]
    assert names == ["send_kyc_resume_link", "retrigger_bank_verification", "create_device_replacement"]
    dev = d["actions"][2]
    assert dev["cls"] == "R2" and dev["status"] == "OK" and dev["undoable"] is True
    assert dev["compensable_until"] == "2026-11-01T11:00:00+05:30"
    assert d["actions"][0]["undoable"] is False and d["actions"][0]["compensable_until"] is None
    assert d["claims"] == []
    assert all(set(m) == {"ts_sim", "direction", "language", "text"} for m in d["messages"])
    assert api.get("/api/cases/CS-9999").status_code == 404


def test_claims_are_derived_from_verify_entries(api, world):
    run_s1_to_device(world)
    Ledger(world.conn, world.clock).append(kind="VERIFY", actor="AGENT", case_id="CS-0001", payload={"claims": [
        {"id": "c1", "text": "Settlement under review", "source": "tool:get_settlements", "level": "CORROBORATED", "rule_id": "RISK-HOLD-001"},
        {"id": "c2", "text": "It will reach tomorrow", "source": "", "level": "UNSUPPORTED", "reason": "no source"}]})
    claims = api.get("/api/cases/CS-0001").json()["claims"]
    assert [(c["id"], c["level"], c["color"], c["reason"], c["rule_id"]) for c in claims] == [
        ("c1", "CORROBORATED", "green", None, "RISK-HOLD-001"), ("c2", "UNSUPPORTED", "red", "no source", None)]


def test_a_verified_claim_that_was_not_sent_is_amber_and_renders_carry_the_back_translation(api, world):
    run_s1_to_device(world)
    Ledger(world.conn, world.clock).append(kind="VERIFY", actor="SYSTEM", case_id="CS-0001", payload={"status": "ESCALATE", "language": "hi-IN", "claims": [
        {"id": "c1", "text": "KYC is APPROVED.", "source": "tool:get_kyc_status#r1", "level": "SUPPORTED", "sent": False, "nli_label": "ENTAILMENT",
         "nli_score": 0.97, "snippet": "Kyc status is APPROVED.", "corroborated_by": []},
        {"id": "c2", "text": "KYC is done tomorrow.", "source": "tool:get_kyc_status#r1", "level": "UNSUPPORTED", "reason": "numbers_not_in_source: 9"}],
        "sentences": [{"kind": "claim", "text": "आपका KYC मंज़ूर है।", "claim_ids": ["c1"], "ok": False, "back_translation": "Your KYC is approved."}]})
    d = api.get("/api/cases/CS-0001").json()
    by_id = {c["id"]: c for c in d["claims"]}
    assert by_id["c1"]["color"] == "amber" and by_id["c1"]["nli_score"] == 0.97 and by_id["c1"]["snippet"] == "Kyc status is APPROVED."
    assert by_id["c2"]["color"] == "red" and by_id["c2"]["reason"] == "numbers_not_in_source: 9"
    assert d["renders"][-1]["sentences"][0]["back_translation"] == "Your KYC is approved." and d["renders"][-1]["status"] == "ESCALATE"


def test_cost_counts_the_llm_clients_context_hash_tokens(api, world):
    run_s1_to_device(world)
    Ledger(world.conn, world.clock).append(kind="CONTEXT_HASH", actor="SYSTEM", case_id="CS-0001", payload={"purpose": "decision", "tokens_in": 2000, "tokens_out": 300})
    assert api.get("/api/cases/CS-0001/cost").json()["usage"] == {"tokens_in": 2000, "tokens_out": 300}


def test_messages_include_merchant_and_agent_turns(api, world):
    run_s1_to_device(world)
    led = Ledger(world.conn, world.clock)
    led.append(kind="MESSAGE_SENT", actor="AGENT", case_id="CS-0001", payload={"language": "hi-IN", "text": "Namaste"})
    world.clock.advance(timedelta(minutes=1))
    world.inject("E-MSG", "MERCHANT_MESSAGE", payload={"language": "hi-IN", "text": "kya hua?"})
    world.conn.execute("INSERT INTO case_events(case_id, event_id) VALUES ('CS-0001','E-MSG')")
    msgs = api.get("/api/cases/CS-0001").json()["messages"]
    assert [(m["direction"], m["text"], m["language"]) for m in msgs] == [("OUT", "Namaste", "hi-IN"), ("IN", "kya hua?", "hi-IN")]


def test_cost_zero_tokens_and_assumptions(api, world):
    run_s1_to_device(world)
    c = api.get("/api/cases/CS-0001/cost").json()
    assert c["ai_inr"] == 0 and c["usage"] == {"tokens_in": 0, "tokens_out": 0}
    assert c["human_baseline_inr"] == {"low": 24, "mid": 60, "high": 150}
    assert len(c["assumptions"]) >= 2 and any("15" in a and "60" in a for a in c["assumptions"])
    assert api.get("/api/cases/CS-9999/cost").status_code == 404


def test_cost_sums_decision_tokens(api, world):
    run_s1_to_device(world)
    led = Ledger(world.conn, world.clock)
    led.append(kind="DECISION", actor="AGENT", case_id="CS-0001", payload={"tokens_in": 1_000_000, "tokens_out": 500_000})
    led.append(kind="DECISION", actor="AGENT", case_id="CS-0001", payload={"tokens_in": 1000, "tokens_out": 0})
    led.append(kind="DECISION", actor="AGENT", case_id="CS-0001", payload={"note": "no tokens"})
    c = api.get("/api/cases/CS-0001/cost").json()
    assert c["usage"] == {"tokens_in": 1_001_000, "tokens_out": 500_000}
    assert c["ai_inr"] == pytest.approx(15 * 1.001 + 60 * 0.5)


def test_undo_inside_window_then_refused_after_window(api, world):
    run_s1_to_device(world)
    body = {"principal_id": "u1", "role": "support_executive"}
    r = api.post("/api/actions/ACT-0003/undo", json=body).json()
    assert r["status"] == "OK" and r["action_id"] == "ACT-0003" and r["error"] is None and r["violations"] == []
    assert world.conn.execute("SELECT status FROM device_replacements WHERE merchant_id='M-0006'").fetchone()[0] == "CANCELLED"
    assert api.get("/api/cases/CS-0001").json()["actions"][2]["undoable"] is False   # done, no longer undoable


def test_undo_refused_after_window_and_not_listed_undoable(api, world):
    run_s1_to_device(world)
    world.clock.advance(timedelta(hours=25))
    assert api.get("/api/cases/CS-0001").json()["actions"][2]["undoable"] is False
    r = api.post("/api/actions/ACT-0003/undo", json={"principal_id": "u1", "role": "support_executive"}).json()
    assert r["status"] == "REFUSED" and "WINDOW_CLOSED" in r["error"]
    assert [v["rule_id"] for v in r["violations"]] == ["COMP-WINDOW"]
    assert world.conn.execute("SELECT status FROM device_replacements WHERE merchant_id='M-0006'").fetchone()[0] == "CREATED"


def test_undo_unknown_action_and_missing_principal(api, world):
    r = api.post("/api/actions/ACT-4242/undo", json={"principal_id": "u1", "role": "support_executive"}).json()
    assert r["status"] == "ERROR" and "NOT_COMPENSABLE" in r["error"]
    assert api.post("/api/actions/ACT-0003/undo", json={}).status_code == 422


def test_approvals_list_and_root_router_mounted(api, world):
    assert api.get("/api/approvals").json() == {"approvals": []}
    import json
    view = {"action": "apply_fee_waiver", "case_id": "CS-0001"}
    world.conn.execute("INSERT INTO approvals(approval_id, case_id, action_id, status, view_json, role_required) VALUES ('AP-0001','CS-0001',NULL,'PENDING',?, 'sales_lead')", (json.dumps(view),))
    world.conn.execute("INSERT INTO approvals(approval_id, case_id, action_id, status, view_json, role_required) VALUES ('AP-0002','CS-0001',NULL,'APPROVED',?, 'sales_lead')", (json.dumps(view),))
    assert api.get("/api/approvals?status=PENDING").json() == {"approvals": [
        {"approval_id": "AP-0001", "case_id": "CS-0001", "action": "apply_fee_waiver", "status": "PENDING", "role_required": "sales_lead"}]}
    assert [a["approval_id"] for a in api.get("/api/approvals?status=").json()["approvals"]] == ["AP-0001", "AP-0002"]
    assert api.get("/approvals/AP-9999").status_code == 404      # the read-only root router is mounted
