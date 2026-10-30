"""Control endpoints: kill switch (end to end with a real Executor), clock, chaos, customer pays, control state."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.actions.types import ActionIntent, Principal
from core.api.app import create_api
from tests.checkpoint_script import SCRIPT
from tests.world import World

HUMAN = Principal("HUMAN", "u1", "support_executive")


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def hook_calls():
    return []


@pytest.fixture
def api(world, hook_calls):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http, after_write=lambda: hook_calls.append(1)))


def write_intent(case_id="CS-T", step_seq=1):
    return ActionIntent("send_kyc_resume_link", {"merchant_id": "M-0006"}, case_id=case_id, step_seq=step_seq, principal=HUMAN, channel="app")


def test_control_initial_state(api):
    assert api.get("/api/control").json() == {"kill_switch": False, "sim_time": "2026-10-31T09:00:00+05:30", "chaos": []}


def test_kill_switch_end_to_end_with_real_executor(api, world):
    # baseline: a write works
    ok = world.executor.submit(write_intent(step_seq=1))
    assert ok.status == "OK", (ok.status, ok.error)
    links = lambda: world.conn.execute("SELECT COUNT(*) FROM links WHERE kind='KYC_RESUME'").fetchone()[0]  # noqa: E731
    assert links() == 1

    assert api.post("/api/control/kill-switch", json={"on": True}).json() == {"kill_switch": True}
    assert api.get("/api/control").json()["kill_switch"] is True
    assert world.conn.execute("SELECT value FROM meta WHERE key='kill_switch'").fetchone()[0] == "on"
    blocked = world.executor.submit(write_intent(step_seq=2))           # immediately: no restart, no new executor
    assert blocked.status == "REFUSED" and "KILL_SWITCH" in blocked.error
    assert [v.rule_id for v in blocked.violations] == ["KILL_SWITCH"]
    assert links() == 1                                                  # nothing was sent

    assert api.post("/api/control/kill-switch", json={"on": False}).json() == {"kill_switch": False}
    assert world.executor.submit(write_intent(step_seq=2)).status == "OK"
    assert links() == 2


def test_kill_switch_is_ledgered(api, world):
    api.post("/api/control/kill-switch", json={"on": True})
    last = world.conn.execute("SELECT kind, payload_json FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    assert last["kind"] == "HUMAN_ACTION" and '"on":true' in last["payload_json"]


def test_clock_advances_and_fires_hook(api, world, hook_calls):
    r = api.post("/api/control/clock", json={"advance_hours": 48}).json()
    assert r == {"sim_time": "2026-11-02T09:00:00+05:30"}
    assert world.clock.now_iso() == "2026-11-02T09:00:00+05:30"
    assert hook_calls == [1]
    assert api.post("/api/control/clock", json={"advance_hours": -1}).status_code == 422
    assert world.clock.now_iso() == "2026-11-02T09:00:00+05:30"
    assert api.post("/api/control/clock", json={"advance_hours": 0.5}).json() == {"sim_time": "2026-11-02T09:30:00+05:30"}


def test_chaos_set_list_and_clear(api, world):
    assert api.post("/api/control/chaos", json={"system": "payments", "mode": "latency", "rate": 0.5, "param": 200}).json() == {"ok": True}
    assert api.get("/api/control").json()["chaos"] == [{"system": "payments", "mode": "latency", "rate": 0.5, "param": 200.0}]
    assert api.post("/api/control/chaos", json={"system": "payments", "mode": "latency", "rate": 0, "param": None}).json() == {"ok": True}
    assert api.get("/api/control").json()["chaos"] == []
    assert api.post("/api/control/chaos", json={"system": "nope", "mode": "latency", "rate": 1}).status_code == 422
    assert api.post("/api/control/chaos", json={"system": "payments", "mode": "latency", "rate": 2}).status_code == 422


def test_chaos_really_reaches_the_mock(api, world):
    api.post("/api/control/chaos", json={"system": "payments", "mode": "error500", "rate": 1.0})
    r = api.post("/api/control/pay", json={"merchant_id": "M-0006", "amount_paise": 24000})
    assert r.status_code == 500
    assert world.conn.execute("SELECT COUNT(*) FROM payments WHERE merchant_id='M-0006'").fetchone()[0] == 0


def test_pay_creates_payment_and_settlement_and_event(api, world, hook_calls):
    r = api.post("/api/control/pay", json={"merchant_id": "M-0006", "amount_paise": 24000}).json()
    assert r["status"] == "SUCCESS" and r["payment_id"] and r["settlement_id"]
    p = world.conn.execute("SELECT * FROM payments WHERE payment_id = ?", (r["payment_id"],)).fetchone()
    assert (p["merchant_id"], p["amount_paise"], p["settlement_id"]) == ("M-0006", 24000, r["settlement_id"])
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE type='PAYMENT_SUCCESS' AND merchant_id='M-0006'").fetchone()[0] == 1
    assert hook_calls == [1]
    # a second click is a second payment (fresh idempotency key), added to the same pending settlement
    r2 = api.post("/api/control/pay", json={"merchant_id": "M-0006", "amount_paise": 10000}).json()
    assert r2["payment_id"] != r["payment_id"] and r2["settlement_id"] == r["settlement_id"]
    assert world.conn.execute("SELECT amount_paise FROM settlements WHERE settlement_id=?", (r["settlement_id"],)).fetchone()[0] == 34000


def test_pay_validation(api):
    assert api.post("/api/control/pay", json={"merchant_id": "M-0006", "amount_paise": 0}).status_code == 422
    assert api.post("/api/control/pay", json={"merchant_id": "M-9999", "amount_paise": 100}).status_code == 404


def test_pay_does_not_go_through_the_executor(api, world):
    before = world.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    api.post("/api/control/pay", json={"merchant_id": "M-0006", "amount_paise": 24000})
    assert world.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == before
