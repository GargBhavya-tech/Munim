import json
import socket
from datetime import timedelta

import pytest

from core.audit.ledger import Ledger
from core.audit.replay import ReplayError, replay_case
from core.cli import main
from core.engine.clock import SimClock
from core.memory.store import connect


def script_case(conn, clock, case_id="C-1042"):
    led = Ledger(conn, clock)

    def step(minutes, **kw):
        clock.advance(timedelta(minutes=minutes))
        led.append(case_id=case_id, **kw)

    step(0, kind="EVENT", actor="SYSTEM", payload={"type": "SIGNUP_STALLED", "event_id": "E-1"})
    step(1, kind="STATE", actor="ENGINE", payload={"state": "ACTIVE", "step": "kyc"})
    step(1, kind="DECISION", actor="MODEL", model_id="sarvam-105b", prompt_hash="ab12", payload={"next_action": "send_kyc_resume_link", "latency_ms": 812.4})
    step(1, kind="POLICY", actor="POLICY", payload={"action": "send_kyc_resume_link", "allow": True, "rule_ids": []})
    step(1, kind="ACTION_INTENT", actor="ENGINE", payload={"action": "send_kyc_resume_link", "key": "k1"})
    step(1, kind="ACTION_RESULT", actor="ENGINE", payload={"action": "send_kyc_resume_link", "key": "k1", "status": 200})
    step(1, kind="MESSAGE_SENT", actor="ENGINE", payload={"language": "kn-IN", "text": "ನಮಸ್ಕಾರ"})
    led.append(kind="EVENT", actor="SYSTEM", case_id="C-OTHER", payload={"type": "PAYMENT_SUCCESS"})  # another case interleaved
    step(120, kind="STATE", actor="ENGINE", payload={"state": "RESOLVED", "step": None})


def test_replay_rebuilds_the_case_state(conn, clock):
    script_case(conn, clock)
    r = replay_case(conn, "C-1042")
    assert r.chain.ok
    assert r.state["status"] == "RESOLVED"
    assert r.state["events"] == ["SIGNUP_STALLED"]
    assert r.state["decisions"] == 1
    assert r.state["actions"] == [{"action": "send_kyc_resume_link", "key": "k1", "status": 200}]
    assert r.state["messages"][0]["language"] == "kn-IN"
    assert len(r.steps) == 8 and "C-OTHER" not in json.dumps(r.steps)


def test_replay_is_deterministic_and_matches_the_stored_hashes(conn, clock):
    script_case(conn, clock)
    a, b = replay_case(conn, "C-1042"), replay_case(conn, "C-1042")
    assert a.state_hash == b.state_hash and a.entry_hashes == b.entry_hashes
    stored = [r["hash"] for r in conn.execute("SELECT hash FROM ledger WHERE case_id='C-1042' ORDER BY seq")]
    assert a.entry_hashes == stored


def test_replay_works_with_the_network_blocked(conn, clock, monkeypatch):
    script_case(conn, clock)

    def no_network(*a, **k):
        raise AssertionError("replay touched the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert replay_case(conn, "C-1042").state["status"] == "RESOLVED"


def test_replay_never_imports_the_model_layer():
    import core.audit.replay as mod

    src = open(mod.__file__).read()
    assert "core.llm" not in src and "httpx" not in src and "requests" not in src


def test_unknown_case_raises(conn):
    with pytest.raises(ReplayError):
        replay_case(conn, "C-NOPE")


def test_replay_reports_a_tampered_chain(conn, clock, tmp_path, db_path):
    script_case(conn, clock)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("DROP TRIGGER ledger_no_update")
    conn.execute("UPDATE ledger SET payload_json = replace(payload_json, 'RESOLVED', 'ESCALATED') WHERE seq = 9")
    r = replay_case(conn, "C-1042")
    assert not r.chain.ok and r.chain.first_mismatch == 9


def test_cli_replay_and_verify(db_path, conn, clock, capsys):
    script_case(conn, clock)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert main(["--db", str(db_path), "verify-ledger"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["--db", str(db_path), "replay", "C-1042"]) == 0
    out = capsys.readouterr().out
    assert "final_status" in out and "RESOLVED" in out
    assert main(["--db", str(db_path), "replay", "C-NOPE"]) == 2
