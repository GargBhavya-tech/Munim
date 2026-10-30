"""Ledger pagination, verification (clean and tampered) and the handoff handlers."""
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.ledger import Ledger
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    w.inject("E-STALL", "SIGNUP_STALLED")
    w.dispatcher.run()
    return w


@pytest.fixture
def api(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http))


def total(world):
    return world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]


def test_pagination_is_complete_ordered_and_bounded(api, world):
    n = total(world)
    assert n > 10
    seen, after = [], 0
    while True:
        r = api.get(f"/api/ledger?after_seq={after}&limit=7").json()
        assert len(r["entries"]) <= 7
        if not r["entries"]:
            assert r["next_after_seq"] == after
            break
        seen += [e["seq"] for e in r["entries"]]
        assert r["next_after_seq"] == r["entries"][-1]["seq"]
        after = r["next_after_seq"]
    assert seen == list(range(1, n + 1))


def test_default_limit_and_hard_cap(api, world):
    assert len(api.get("/api/ledger").json()["entries"]) == min(100, total(world))
    assert api.get("/api/ledger?limit=100000").status_code == 422     # never the whole ledger in one response
    assert api.get("/api/ledger?limit=0").status_code == 422


def test_case_filter(api, world):
    r = api.get("/api/ledger?case_id=CS-0001&limit=500").json()
    assert r["entries"] and all(e["case_id"] == "CS-0001" for e in r["entries"])
    assert api.get("/api/ledger?case_id=CS-0404").json() == {"entries": [], "next_after_seq": 0}


def test_entry_shape_matches_database_row(api, world):
    e = api.get("/api/ledger?limit=1").json()["entries"][0]
    row = world.conn.execute("SELECT * FROM ledger WHERE seq = 1").fetchone()
    assert (e["seq"], e["hash"], e["kind"], e["actor"], e["ts_sim"]) == (1, row["hash"], row["kind"], row["actor"], row["ts_sim"])
    assert isinstance(e["payload"], dict)


def test_verify_clean(api, world):
    assert api.get("/api/ledger/verify").json() == {"ok": True, "entries": total(world), "first_mismatch": None, "reason": None}


def test_verify_reports_first_mismatch_on_tampered_copy(world, tmp_path):
    copy = tmp_path / "tampered.db"
    world.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(world.db_path, copy)
    c = sqlite3.connect(copy)
    c.execute("DROP TRIGGER ledger_no_update")
    c.execute("UPDATE ledger SET actor = 'EVIL' WHERE seq = 5")
    c.commit()
    c.close()
    api = TestClient(create_api(copy, world.clock, world.executor, world.http))
    r = api.get("/api/ledger/verify").json()
    assert r["ok"] is False and r["first_mismatch"] == 5 and "hash" in r["reason"]
    # the untouched original still verifies
    assert TestClient(create_api(world.db_path, world.clock, world.executor, world.http)).get("/api/ledger/verify").json()["ok"] is True


# ----------------------------------------------------------------------------------------------- handoffs
def add_handoff(world, hid="H-0001", status="OPEN"):
    world.conn.execute("INSERT INTO handoffs(handoff_id, case_id, packet_json, status, owner_role, deadline, created_at) VALUES (?, 'CS-0001', ?, ?, 'support_executive', '2026-10-31T13:00:00+05:30', '2026-10-31T09:00:00+05:30')",
                       (hid, '{"summary":"name mismatch"}', status))


def test_handoff_list_and_status_filter(api, world):
    add_handoff(world, "H-0001", "OPEN")
    add_handoff(world, "H-0002", "RETURNED")
    hs = api.get("/api/handoffs").json()["handoffs"]
    assert [h["handoff_id"] for h in hs] == ["H-0001", "H-0002"]
    assert hs[0] == {"handoff_id": "H-0001", "case_id": "CS-0001", "packet": {"summary": "name mismatch"}, "status": "OPEN",
                     "owner_role": "support_executive", "deadline": "2026-10-31T13:00:00+05:30", "created_at": "2026-10-31T09:00:00+05:30", "verdict": None}
    assert [h["handoff_id"] for h in api.get("/api/handoffs?status=RETURNED").json()["handoffs"]] == ["H-0002"]


def test_handoff_return_writes_event_and_ledger(api, world):
    add_handoff(world)
    r = api.post("/api/handoffs/H-0001/return", json={"instruction": "name matches PAN, proceed", "principal_id": "u7", "role": "support_executive"})
    assert r.json() == {"ok": True, "status": "RETURNED"}
    ev = world.conn.execute("SELECT * FROM events WHERE type='HUMAN_RETURNED'").fetchall()
    assert len(ev) == 1 and ev[0]["case_id"] == "CS-0001" and ev[0]["merchant_id"] == "M-0006"
    last = world.conn.execute("SELECT * FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    assert (last["kind"], last["actor"], last["role"], last["case_id"]) == ("HUMAN_ACTION", "HUMAN:u7", "support_executive", "CS-0001")
    assert api.get("/api/handoffs?status=RETURNED").json()["handoffs"][0]["handoff_id"] == "H-0001"
    # second return is a no-op that reports the status
    r2 = api.post("/api/handoffs/H-0001/return", json={"instruction": "again", "principal_id": "u7", "role": "support_executive"})
    assert r2.json() == {"ok": False, "status": "RETURNED"}
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE type='HUMAN_RETURNED'").fetchone()[0] == 1


def test_handoff_return_refuses_auditor_and_unknown(api, world):
    add_handoff(world)
    assert api.post("/api/handoffs/H-0001/return", json={"instruction": "x", "principal_id": "a", "role": "auditor"}).status_code == 403
    assert api.post("/api/handoffs/H-0001/return", json={"instruction": "x", "principal_id": "a", "role": "wizard"}).status_code == 403
    assert api.post("/api/handoffs/H-0404/return", json={"instruction": "x", "principal_id": "a", "role": "ops_admin"}).status_code == 404
    assert world.conn.execute("SELECT status FROM handoffs").fetchone()[0] == "OPEN"
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE type='HUMAN_RETURNED'").fetchone()[0] == 0


def test_handoff_verdict(api, world):
    add_handoff(world)
    assert api.post("/api/handoffs/H-0001/verdict", json={"verdict": "needed"}).json() == {"ok": True}
    assert api.get("/api/handoffs").json()["handoffs"][0]["verdict"] == "needed"
    assert api.post("/api/handoffs/H-0001/verdict", json={"verdict": "maybe"}).status_code == 422
    assert api.post("/api/handoffs/H-0404/verdict", json={"verdict": "needed"}).status_code == 404
    assert world.conn.execute("SELECT payload_json FROM ledger WHERE kind='HUMAN_ACTION' ORDER BY seq DESC LIMIT 1").fetchone()[0].count("needed") == 1
