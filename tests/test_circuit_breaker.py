"""#28: per-system breaker, persisted, driven by the SimClock."""
from datetime import timedelta

from core.actions.circuit_breaker import CircuitBreaker
from core.actions.types import ActionIntent
from core.audit.ledger import Ledger
from core.memory.store import connect
from tests.test_executor import M, SpyHttp, chaos, make_executor, n, waiver


def breaker_events(conn):
    rows = conn.execute("SELECT payload_json FROM ledger WHERE kind='STATE' ORDER BY seq").fetchall()
    return [(p["breaker"], p["from"], p["to"]) for p in (__import__("json").loads(r[0]) for r in rows)]


def test_opens_after_three_consecutive_failures_probes_after_30s_sim_and_closes(db_path, clock, http, conn):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    chaos(http, "pricing", "error500", 1.0)
    for i in range(3):
        assert e.breaker.state("pricing") == "CLOSED"
        assert e.submit(waiver(seq=i)).error.startswith("UPSTREAM_UNAVAILABLE")
    assert e.breaker.state("pricing") == "OPEN"
    assert breaker_events(conn) == [("pricing", "CLOSED", "OPEN")]

    calls = len(spy.calls)
    clock.advance(timedelta(seconds=29))
    out = e.submit(waiver(seq=10))
    assert out.error.startswith("CIRCUIT_OPEN") and len(spy.calls) == calls          # 29 s: still open, mock untouched

    clock.advance(timedelta(seconds=1))                                                                  # 30 s total
    probe = e.submit(waiver(seq=11))                                                  # still failing -> probe fails -> reopen
    assert len(spy.calls) == calls + 1                                                # ONE probe attempt, no retries
    assert probe.error.startswith("UPSTREAM_UNAVAILABLE") and e.breaker.state("pricing") == "OPEN"
    assert breaker_events(conn)[-2:] == [("pricing", "OPEN", "HALF_OPEN"), ("pricing", "HALF_OPEN", "OPEN")]
    assert e.submit(waiver(seq=12)).error.startswith("CIRCUIT_OPEN")                  # cooldown restarted from the failed probe

    chaos(http, "pricing", "error500", 0)
    clock.advance(timedelta(seconds=30))
    base = n(conn, "offers")
    ok = e.submit(waiver(seq=13))
    assert ok.status == "OK" and n(conn, "offers") == base + 1
    assert e.breaker.state("pricing") == "CLOSED" and e.breaker.failures("pricing") == 0
    assert breaker_events(conn)[-2:] == [("pricing", "OPEN", "HALF_OPEN"), ("pricing", "HALF_OPEN", "CLOSED")]


def test_success_resets_the_consecutive_count(db_path, clock, http):
    e = make_executor(db_path, clock, SpyHttp(http))
    chaos(http, "pricing", "error500", 1.0)
    e.submit(waiver(seq=0)); e.submit(waiver(seq=1))
    assert e.breaker.failures("pricing") == 2
    chaos(http, "pricing", "error500", 0)
    assert e.submit(waiver(seq=2)).status == "OK"
    assert e.breaker.failures("pricing") == 0
    chaos(http, "pricing", "error500", 1.0)
    e.submit(waiver(seq=3)); e.submit(waiver(seq=4))
    assert e.breaker.state("pricing") == "CLOSED"            # 2 since the success, not 4


def test_breaker_is_per_system(db_path, clock, http):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    chaos(http, "pricing", "error500", 1.0)
    for i in range(3):
        e.submit(waiver(seq=i))
    assert e.breaker.state("pricing") == "OPEN"
    t = e.submit(ActionIntent("raise_ticket", {"merchant_id": M, "type": "DEVICE"}, case_id="C-1"))
    assert t.status == "OK" and e.breaker.state("tickets") == "CLOSED"


def test_state_survives_a_restart(db_path, clock, http):
    e1 = make_executor(db_path, clock, SpyHttp(http))
    chaos(http, "pricing", "error500", 1.0)
    for i in range(3):
        e1.submit(waiver(seq=i))
    e1.conn.close()
    spy = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy)                  # brand new process state
    assert e2.breaker.state("pricing") == "OPEN"
    assert e2.submit(waiver(seq=5)).error.startswith("CIRCUIT_OPEN") and spy.calls == []
    row = e2.conn.execute("SELECT state, failures, opened_at FROM breaker_state WHERE system='pricing'").fetchone()
    assert row["state"] == "OPEN" and row["failures"] == 3 and row["opened_at"] == "2026-10-31T09:00:00+05:30"


def test_every_state_change_is_ledgered_and_chain_verifies(db_path, clock, http, conn):
    from core.audit.ledger import verify_chain
    e = make_executor(db_path, clock, SpyHttp(http))
    chaos(http, "pricing", "error500", 1.0)
    for i in range(3):
        e.submit(waiver(seq=i))
    assert breaker_events(conn) == [("pricing", "CLOSED", "OPEN")]
    entry = [x for x in e.ledger.entries() if x.kind == "STATE"][0]
    assert entry.actor == "SYSTEM" and entry.case_id is None and entry.payload["failures"] == 3
    assert verify_chain(conn).ok


def test_reads_trip_the_breaker_too_and_a_dead_read_gets_an_answer(db_path, clock, http):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    chaos(http, "pricing", "error500", 1.0)
    for _ in range(3):
        assert e.read("get_offer_band", {"merchant_id": M}).error.startswith("UPSTREAM_UNAVAILABLE")
    calls = len(spy.calls)
    assert e.read("get_offer_band", {"merchant_id": M}).error.startswith("CIRCUIT_OPEN") and len(spy.calls) == calls


def test_standalone_breaker_unit(conn, clock):
    b = CircuitBreaker(conn, clock, Ledger(conn, clock), failure_threshold=2, cooldown_s=10)
    assert b.allow("x") == (True, False)
    b.record_failure("x"); b.record_failure("x")
    assert b.allow("x") == (False, False) and b.retry_after_s("x") == 10
    clock.advance(timedelta(seconds=10))
    assert b.allow("x") == (True, True) and b.state("x") == "HALF_OPEN"
    b.record_success("x")
    assert b.state("x") == "CLOSED"
