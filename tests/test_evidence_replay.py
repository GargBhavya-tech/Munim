"""#67 replay from the API, #136 evidence pack: complete, redacted, and self-verifying (an edited pack fails on its own)."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.evidence import build_pack, render_html, verify_pack
from tests.checkpoint_script import SCRIPT
from tests.test_api_cases import run_s1_to_device
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def api(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http, registry=world.registry))


def test_replay_endpoint_rebuilds_the_case_from_the_ledger(api, world):
    run_s1_to_device(world)
    r = api.get("/api/cases/CS-0001/replay").json()
    assert r["chain"]["ok"] is True and r["state"]["status"] == world.case().state
    assert [a["action"] for a in r["state"]["actions"] if a["status"] == "OK"][:1] == ["send_kyc_resume_link"]
    assert len(r["state_hash"]) == 64 and r["steps"][0]["kind"] == "STATE"
    assert api.get("/api/cases/CS-0001/replay").json()["state_hash"] == r["state_hash"]          # deterministic
    assert api.get("/api/cases/CS-9999/replay").status_code == 404


def test_the_evidence_pack_verifies_its_ledger_slice_and_an_edit_is_caught(world):
    run_s1_to_device(world)
    pack = build_pack(world.conn, "CS-0001")
    assert verify_pack(pack) == (True, None)
    assert {a["name"] for a in pack["actions"]} >= {"send_kyc_resume_link"} and all(a["idempotency_key"] for a in pack["actions"])
    tampered = copy.deepcopy(pack)
    victim = next(e for e in tampered["ledger_slice"] if e["kind"] == "ACTION_RESULT")
    victim["payload"]["status"] = "ERROR"
    assert verify_pack(tampered) == (False, victim["seq"])
    json.dumps(pack)                                                                  # it is exportable as JSON


def test_the_export_never_carries_unredacted_merchant_text(world):
    run_s1_to_device(world)
    world.inject("E-LEAK", "MERCHANT_MESSAGE", payload={"text": "mera OTP 482913 hai aur phone 9876543210"})
    world.conn.execute("INSERT INTO case_events(case_id, event_id) VALUES ('CS-0001', 'E-LEAK')")
    pack = build_pack(world.conn, "CS-0001")
    page = render_html(pack)
    assert any(m["direction"] == "IN" for m in pack["transcript"])
    for raw in ("482913", "9876543210"):
        assert raw not in json.dumps(pack, ensure_ascii=False) and raw not in page


def test_the_html_pack_is_served_and_says_verified(api, world):
    run_s1_to_device(world)
    r = api.get("/api/cases/CS-0001/evidence")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "SIMULATED" in r.text and "<b>VERIFIED</b>" in r.text
    assert api.get("/api/cases/CS-0001/evidence?format=json").json()["chain"]["ok"] is True
