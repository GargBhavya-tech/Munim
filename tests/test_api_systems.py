"""Systems snapshot: read-only observability over the shared database."""
import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def api(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http))


def test_snapshot_keys(api):
    s = api.get("/api/merchants/M-0006/systems").json()
    assert set(s) == {"kyc", "bank", "device", "payments", "crm", "pricing", "scheduler", "tickets", "comms", "handoff_queue"}


def test_m0006_stalled_signup(api):
    s = api.get("/api/merchants/M-0006/systems").json()
    assert (s["kyc"]["status"], s["kyc"]["reason"], s["kyc"]["attempts"]) == ("REJECTED", "DOC_BLURRY", 1)
    assert (s["bank"]["status"], s["bank"]["attempts"]) == ("PENDING", 0)
    assert s["device"]["status"] == "OFFLINE" and s["device"]["last_test_announcement_ok"] == 0
    assert s["payments"]["latest"] == [] and s["payments"]["settlement"] is None
    assert s["crm"]["open_cases"] == [] and s["crm"]["focus"] is None and s["crm"]["language"] == "kn-IN"
    assert s["tickets"] == [] and s["handoff_queue"] == {"open": 0}


def test_m0002_held_settlement_and_complaint(api):
    s = api.get("/api/merchants/M-0002/systems").json()
    st = s["payments"]["settlement"]
    assert (st["status"], st["amount_paise"], st["reason"]) == ("HELD_RISK", 1240000, "RISK_REVIEW")
    assert [(t["type"], t["status"]) for t in s["tickets"]] == [("COMPLAINT", "OPEN")]
    assert s["tickets"][0]["system_object"] == st["settlement_id"]
    assert len(s["payments"]["latest"]) == 5
    assert s["payments"]["latest"] == sorted(s["payments"]["latest"], key=lambda p: (p["ts"], p["payment_id"]), reverse=True)
    assert s["pricing"]["band"]["max_months"] >= 1


def test_snapshot_follows_state_changes_and_open_cases(api, world):
    world.inject("E-STALL", "SIGNUP_STALLED")
    world.dispatcher.run()
    s = api.get("/api/merchants/M-0006/systems").json()
    assert [c["case_id"] for c in s["crm"]["open_cases"]] == ["CS-0001"]
    assert s["crm"]["focus"] == "S1_ACTIVATION:kyc"
    assert len(s["comms"]) <= 10
    world.control("/mock/control/kyc/M-0006", status="APPROVED")
    assert api.get("/api/merchants/M-0006/systems").json()["kyc"]["status"] == "APPROVED"


def test_handoff_queue_counts_open_only(api, world):
    world.inject("E-STALL", "SIGNUP_STALLED")
    world.dispatcher.run()
    for hid, status in (("H-1", "OPEN"), ("H-2", "RETURNED")):
        world.conn.execute("INSERT INTO handoffs(handoff_id, case_id, packet_json, status, created_at) VALUES (?, 'CS-0001', '{}', ?, '2026-10-31T09:00:00+05:30')", (hid, status))
    assert api.get("/api/merchants/M-0006/systems").json()["handoff_queue"] == {"open": 1}


def test_unknown_merchant_404(api):
    assert api.get("/api/merchants/M-9999/systems").status_code == 404


def test_systems_endpoint_does_not_write(api, world):
    before = world.conn.execute("SELECT (SELECT COUNT(*) FROM ledger) + (SELECT COUNT(*) FROM actions) + (SELECT COUNT(*) FROM kyc)").fetchone()[0]
    api.get("/api/merchants/M-0002/systems")
    assert world.conn.execute("SELECT (SELECT COUNT(*) FROM ledger) + (SELECT COUNT(*) FROM actions) + (SELECT COUNT(*) FROM kyc)").fetchone()[0] == before
