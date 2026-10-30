import json
import threading
from datetime import timedelta

import pytest

from core.audit.ledger import GENESIS_HASH, Ledger, canonical_json, verify_chain
from core.engine.clock import SimClock
from core.memory.store import connect, copy_db


@pytest.fixture
def ledger(conn):
    return Ledger(conn, SimClock())


def fill(ledger, n=40):
    for i in range(n):
        ledger.clock.advance(timedelta(minutes=1))
        ledger.append(kind="ACTION_RESULT", actor="ENGINE", case_id=f"C-{i % 3}", payload={"i": i, "amount": 100 * i, "text": "ಕನ್ನಡ"})


def test_forty_entries_verify(ledger, conn):
    fill(ledger)
    r = verify_chain(conn)
    assert r.ok and r.entries == 40


def test_empty_ledger_verifies(conn):
    assert verify_chain(conn).ok


def test_first_entry_chains_from_genesis(ledger):
    e = ledger.append(kind="STATE", actor="ENGINE")
    assert e.prev_hash == GENESIS_HASH and e.seq == 1


def test_tamper_on_copy_reports_exact_first_bad_seq(ledger, conn, tmp_path, db_path):
    fill(ledger)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    copy = tmp_path / "tampered.db"
    copy_db(db_path, copy)
    c = connect(copy)
    c.execute("DROP TRIGGER ledger_no_update")  # tampering needs deliberate effort, and only on a copy
    row = c.execute("SELECT payload_json FROM ledger WHERE seq = 17").fetchone()
    payload = json.loads(row["payload_json"])
    payload["amount"] = 999999
    c.execute("UPDATE ledger SET payload_json = ? WHERE seq = 17", (canonical_json(payload),))
    r = verify_chain(c)
    assert not r.ok and r.first_mismatch == 17
    assert verify_chain(conn).ok  # the original is untouched


def test_deleting_an_entry_is_detected(ledger, conn, tmp_path, db_path):
    fill(ledger, 10)
    copy = tmp_path / "gap.db"
    copy_db(db_path, copy)
    c = connect(copy)
    c.execute("DROP TRIGGER ledger_no_delete")
    c.execute("DELETE FROM ledger WHERE seq = 5")
    r = verify_chain(c)
    assert not r.ok and r.first_mismatch == 5


def test_ledger_rejects_update_and_delete(ledger, conn):
    fill(ledger, 3)
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE ledger SET actor='X' WHERE seq=1")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM ledger WHERE seq=1")


def test_key_order_does_not_change_hashes(conn):
    clock = SimClock()
    a = Ledger(conn, clock).append(kind="STATE", actor="E", payload={"a": 1, "b": {"x": 1, "y": 2}})
    copy_clock = SimClock()
    conn.execute("DROP TRIGGER ledger_no_delete")
    conn.execute("DELETE FROM ledger")
    b = Ledger(conn, copy_clock).append(kind="STATE", actor="E", payload={"b": {"y": 2, "x": 1}, "a": 1})
    assert a.hash == b.hash


def test_volatile_fields_are_stored_but_not_hashed(conn):
    clock = SimClock()
    a = Ledger(conn, clock).append(kind="DECISION", actor="E", payload={"n": 1, "latency_ms": 12.5, "model_latency_ms": 3})
    conn.execute("DROP TRIGGER ledger_no_delete")
    conn.execute("DELETE FROM ledger")
    b = Ledger(conn, SimClock()).append(kind="DECISION", actor="E", payload={"n": 1, "latency_ms": 999.0, "model_latency_ms": 1})
    assert a.hash == b.hash  # latency differs, real timestamps differ, hash identical
    assert verify_chain(conn).ok


def test_unknown_kind_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.append(kind="WHATEVER", actor="E")


def test_ledger_joins_callers_transaction_and_rolls_back_with_it(ledger, conn):
    conn.execute("BEGIN IMMEDIATE")
    ledger.append(kind="STATE", actor="E")
    conn.execute("ROLLBACK")
    assert verify_chain(conn).entries == 0


def test_concurrent_writers_never_interleave(db_path):
    clock = SimClock()
    errors = []

    def worker(n):
        try:
            c = connect(db_path)
            led = Ledger(c, clock)
            for i in range(25):
                led.append(kind="STATE", actor=f"W{n}", payload={"i": i})
            c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    c = connect(db_path)
    r = verify_chain(c)
    assert r.ok and r.entries == 100
