from datetime import timedelta

from core.audit.ledger import Ledger
from core.engine.cases import CaseRepo
from core.engine.clock import SimClock
from core.engine.timers import TimerService

MID = "M-0001"


def setup(conn, clock):
    ledger = Ledger(conn, clock)
    repo = CaseRepo(conn, clock, ledger)
    got = []
    timers = TimerService(conn, clock, ledger, handler=got.append)
    case = repo.transition(repo.open_case(playbook_id="S1", lane="P2", merchant_id=MID), "ACTIVE")
    return ledger, repo, timers, got, case


def n_events(conn):
    return conn.execute("SELECT COUNT(*) FROM events WHERE type='TIMER_FIRED'").fetchone()[0]


def test_advance_48h_fires_a_47h_timer_exactly_once(conn, clock):
    ledger, repo, timers, got, case = setup(conn, clock)
    start = clock.now()
    timers.schedule(case.case_id, start + timedelta(hours=47), "nudge", {"n": 1})
    clock.advance(timedelta(hours=46))
    assert got == [] and n_events(conn) == 0
    clock.advance(timedelta(hours=2))
    assert len(got) == 1 and n_events(conn) == 1
    ev = got[0]
    assert ev["type"] == "TIMER_FIRED" and ev["case_id"] == case.case_id and ev["merchant_id"] == MID
    assert ev["payload"] == {"timer_id": 1, "kind": "nudge", "n": 1}
    clock.advance(timedelta(hours=100))
    assert len(got) == 1 and n_events(conn) == 1
    assert conn.execute("SELECT status FROM timers").fetchone()[0] == "FIRED"
    # the fired timer was appended to the event log with the sim time of its due date
    assert conn.execute("SELECT ts_sim FROM events WHERE type='TIMER_FIRED'").fetchone()[0].startswith("2026-11-02T08:00")


def test_timer_does_not_fire_after_case_closed(conn, clock):
    """Watch-out: closing the case without cancelling must still not fire a stale nudge."""
    ledger, repo, timers, got, case = setup(conn, clock)
    timers.schedule(case.case_id, clock.now() + timedelta(hours=10), "nudge")
    repo.transition(case, "RESOLVED")          # closed WITHOUT anyone cancelling the timer
    clock.advance(timedelta(hours=48))
    assert got == [] and n_events(conn) == 0
    assert conn.execute("SELECT status FROM timers").fetchone()[0] == "CANCELLED"


def test_cancel_for_case_cancels_ledgers_and_never_fires(conn, clock):
    ledger, repo, timers, got, case = setup(conn, clock)
    for h in (5, 10, 20):
        timers.schedule(case.case_id, clock.now() + timedelta(hours=h), "nudge")
    assert timers.cancel_for_case(case.case_id) == 3
    clock.advance(timedelta(hours=48))
    assert got == []
    comps = [e.payload for e in ledger.entries(case.case_id) if e.kind == "COMPENSATION"]
    assert len(comps) == 3 and all(c["action"] == "cancel_timer" and c["status"] == "CANCELLED" for c in comps)
    assert timers.cancel_for_case(case.case_id) == 0   # idempotent


def test_cancel_single_timer_is_a_ledgered_compensation(conn, clock):
    ledger, repo, timers, got, case = setup(conn, clock)
    t = timers.schedule(case.case_id, clock.now() + timedelta(hours=5), "nudge")
    assert timers.cancel(t, reason="merchant_replied") is True
    assert timers.cancel(t) is False
    clock.advance(timedelta(hours=10))
    assert got == []
    c = [e.payload for e in ledger.entries(case.case_id) if e.kind == "COMPENSATION"]
    assert c[0]["reason"] == "merchant_replied" and c[0]["timer_id"] == t


def test_restart_rearms_pending_timers_and_fires_once(conn, clock):
    ledger, repo, timers, got, case = setup(conn, clock)
    timers.schedule(case.case_id, clock.now() + timedelta(hours=30), "nudge")
    # "restart": new clock at the same instant + new service over the same database
    clock2 = SimClock(clock.now())
    got2 = []
    timers2 = TimerService(conn, clock2, Ledger(conn, clock2), handler=got2.append)
    assert timers2.rearm() == 1
    assert timers2.rearm() == 0               # no double registration
    clock2.advance(timedelta(hours=31))
    assert len(got2) == 1 and n_events(conn) == 1


def test_overdue_timer_fires_immediately_on_rearm(conn, clock):
    ledger, repo, timers, got, case = setup(conn, clock)
    timers.schedule(case.case_id, clock.now() + timedelta(hours=1), "nudge")
    clock2 = SimClock(clock.now() + timedelta(hours=5))   # restart came late
    got2 = []
    TimerService(conn, clock2, Ledger(conn, clock2), handler=got2.append).rearm()
    assert len(got2) == 1


def test_cancel_for_subject_only_touches_that_subjects_cases(conn, clock):
    ledger, repo, timers, got, case = setup(conn, clock)
    other = repo.transition(repo.open_case(playbook_id="S1", lane="P2", merchant_id="M-0002"), "ACTIVE")
    timers.schedule(case.case_id, clock.now() + timedelta(hours=5), "nudge")
    timers.schedule(other.case_id, clock.now() + timedelta(hours=5), "nudge")
    assert timers.cancel_for_subject(MID) == 1
    clock.advance(timedelta(hours=6))
    assert [e["case_id"] for e in got] == [other.case_id]
