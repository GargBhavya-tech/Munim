"""WebSocket /ws: hello, push of entries appended after connect, per-case subscription."""
import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.ledger import Ledger
from core.memory.store import connect
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    w.inject("E-STALL", "SIGNUP_STALLED")
    w.dispatcher.run()
    return w


@pytest.fixture
def client(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http, ws_poll_s=0.02))


def other_writer(world):
    """A separate connection, like the engine process writing while the API streams."""
    return Ledger(connect(world.db_path), world.clock)


def test_hello_then_new_entry_pushed_not_history(client, world):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "hello", "sim_time": "2026-10-31T09:00:00+05:30"}
        e = other_writer(world).append(kind="STATE", actor="SYSTEM", case_id="CS-0001", payload={"n": 1})
        msg = ws.receive_json()          # the FIRST message after hello is the new entry: history is not replayed
        assert msg["type"] == "ledger"
        assert (msg["entry"]["seq"], msg["entry"]["hash"], msg["entry"]["payload"], msg["entry"]["kind"]) == (e.seq, e.hash, {"n": 1}, "STATE")
        e2 = other_writer(world).append(kind="STATE", actor="SYSTEM", case_id="CS-0002", payload={"n": 2})
        assert ws.receive_json()["entry"]["seq"] == e2.seq == e.seq + 1


def test_entries_arrive_in_order_and_exactly_once(client, world):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        w = other_writer(world)
        seqs = [w.append(kind="STATE", actor="SYSTEM", case_id="CS-0001", payload={"i": i}).seq for i in range(5)]
        got = [ws.receive_json()["entry"]["seq"] for _ in range(5)]
        assert got == seqs


def test_case_subscription_filters(client, world):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"subscribe": {"case_id": "CS-0001"}})
        w = other_writer(world)
        w.append(kind="STATE", actor="SYSTEM", case_id="CS-0002", payload={"who": "other"})
        w.append(kind="STATE", actor="SYSTEM", case_id=None, payload={"who": "none"})
        mine = w.append(kind="STATE", actor="SYSTEM", case_id="CS-0001", payload={"who": "mine"})
        msg = ws.receive_json()
        assert msg["entry"]["seq"] == mine.seq and msg["entry"]["payload"] == {"who": "mine"}


def test_unsubscribe_back_to_all(client, world):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"subscribe": {"case_id": "CS-0001"}})
        ws.send_json({"subscribe": {"case_id": None}})
        w = other_writer(world)
        e = w.append(kind="STATE", actor="SYSTEM", case_id="CS-0002", payload={})
        assert ws.receive_json()["entry"]["seq"] == e.seq


def test_executor_activity_streams(client, world):
    """A real action through the executor shows up as ledger pushes."""
    from core.actions.types import ActionIntent, Principal
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        out = world.executor.submit(ActionIntent("send_kyc_resume_link", {"merchant_id": "M-0006"}, case_id="CS-0001", step_seq=9,
                                                 principal=Principal("HUMAN", "u1", "support_executive"), channel="app"))
        assert out.status == "OK"
        kinds = [ws.receive_json()["entry"]["kind"] for _ in range(3)]
        assert kinds == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]


def test_malformed_client_frame_does_not_kill_stream(client, world):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(["not", "an", "object"])
        e = other_writer(world).append(kind="STATE", actor="SYSTEM", case_id="CS-0001", payload={})
        assert ws.receive_json()["entry"]["seq"] == e.seq
