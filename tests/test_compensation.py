"""#27: compensation registry and Undo."""
import json
from datetime import timedelta

import pytest

from core.actions.types import ActionIntent, Principal
from tests.test_executor import AGENT, AllowAll, DenyAll, M, SpyHttp, chaos, kinds, make_executor, n, waiver

SNAP_TABLES = ["merchants", "plans", "tickets", "schedules", "bookings", "comms_outbox", "device_replacements", "device_orders", "refunds", "closures", "plan_changes"]


def snap(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in SNAP_TABLES}


@pytest.fixture
def ex(db_path, clock, http):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    e.spy = spy
    return e


def test_success_registers_a_compensation_with_a_window_on_the_sim_clock(ex, clock):
    out = ex.submit(waiver())
    row = ex.compensations.get(out.action_id)
    assert json.loads(row["compensation_json"]) == {"action": "revoke_fee_waiver", "args": {"waiver_id": out.result["waiver_id"]}, "of": "apply_fee_waiver"}
    assert row["status"] == "OPEN" and row["compensable_until"] == "2026-11-07T09:00:00+05:30" == out.compensable_until   # now + 7 days


def test_reads_r3_and_internal_compensations_register_nothing(ex):
    ex.submit(ActionIntent("get_offer_band", {"merchant_id": M}, case_id="C-1"))
    ex.submit(ActionIntent("send_message", {"merchant_id": M, "channel": "app", "body": "x"}, case_id="C-1"))
    ex.submit(ActionIntent("draft_message", {"merchant_id": M, "text": "x"}, case_id="C-1"))
    assert n(ex.conn, "compensations") == 0


def test_waiver_apply_then_undo_restores_the_offers_row_to_revoked_and_changes_nothing_else(ex, conn):
    before = snap(conn)
    offers_before = {tuple(r) for r in conn.execute("SELECT * FROM offers")}
    out = ex.submit(waiver())
    wid = out.result["waiver_id"]
    applied = tuple(conn.execute("SELECT * FROM offers WHERE waiver_id=?", (wid,)).fetchone())
    assert applied[4] == "APPLIED"
    u = ex.undo(out.action_id, AGENT)
    assert u.status == "OK" and u.result == {"waiver_id": wid, "status": "REVOKED"}
    after = tuple(conn.execute("SELECT * FROM offers WHERE waiver_id=?", (wid,)).fetchone())
    assert after == applied[:4] + ("REVOKED",) + applied[5:]                      # only the status moved
    assert {tuple(r) for r in conn.execute("SELECT * FROM offers")} - {after} == offers_before   # every other offer untouched
    assert snap(conn) == before                                                    # and no other table changed
    assert ex.compensations.get(out.action_id)["status"] == "DONE"


def test_undo_twice_is_a_noop_that_returns_the_stored_result(ex):
    out = ex.submit(waiver())
    first = ex.undo(out.action_id, AGENT)
    deletes = [c for c in ex.spy.calls if c[0] == "DELETE"]
    second = ex.undo(out.action_id, AGENT)
    assert first.status == "OK" and second.status == "REPLAYED" and second.result == first.result
    assert second.idempotency_key == first.idempotency_key
    assert [c for c in ex.spy.calls if c[0] == "DELETE"] == deletes and len(deletes) == 1     # the mock was not called again
    assert [e.payload["status"] for e in ex.ledger.entries("C-1") if e.kind == "COMPENSATION"] == ["OK", "REPLAYED"]


def test_undo_survives_a_restart_and_is_still_a_noop(db_path, clock, http):
    e1 = make_executor(db_path, clock, SpyHttp(http))
    out = e1.submit(waiver())
    e1.undo(out.action_id, AGENT)
    e1.conn.close()
    spy = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy)
    assert e2.undo(out.action_id, AGENT).status == "REPLAYED" and spy.calls == []


def test_undo_after_the_window_is_refused_with_a_reason_and_without_calling_the_mock(ex, clock, conn):
    out = ex.submit(ActionIntent("create_device_replacement", {"merchant_id": "M-0001", "reason": "NO_POWER"}, case_id="C-9"))
    assert out.status == "OK" and out.compensable_until == "2026-11-01T09:00:00+05:30"       # 24 h
    clock.advance(timedelta(hours=24, seconds=1))
    calls = len(ex.spy.calls)
    u = ex.undo(out.action_id, AGENT)
    assert u.status == "REFUSED" and u.error.startswith("WINDOW_CLOSED") and "2026-11-01T09:00:00+05:30" in u.error
    assert [v.rule_id for v in u.violations] == ["COMP-WINDOW"]
    assert len(ex.spy.calls) == calls
    rid = out.result["replacement_id"]
    assert conn.execute("SELECT status FROM device_replacements WHERE replacement_id=?", (rid,)).fetchone()[0] != "CANCELLED"
    assert ex.compensations.get(out.action_id)["status"] == "OPEN"
    last = [e for e in ex.ledger.entries("C-9") if e.kind == "COMPENSATION"][-1]
    assert last.payload["status"] == "REFUSED" and "WINDOW_CLOSED" in last.payload["reason"]


def test_undo_exactly_at_the_end_of_the_window_still_works(ex, clock):
    out = ex.submit(ActionIntent("create_device_replacement", {"merchant_id": "M-0001", "reason": "NO_POWER"}, case_id="C-9"))
    clock.advance(timedelta(hours=24))
    assert ex.undo(out.action_id, AGENT).status == "OK"


def test_window_is_checked_in_code_not_left_to_the_mock(ex, clock):
    """The mock has its own window; ours must refuse first. Prove it by making the mock window larger than ours."""
    out = ex.submit(waiver())
    ex.conn.execute("UPDATE compensations SET compensable_until = ? WHERE action_id = ?", ("2026-10-31T10:00:00+05:30", out.action_id))
    clock.advance(timedelta(hours=2))
    calls = len(ex.spy.calls)
    assert ex.undo(out.action_id, AGENT).status == "REFUSED" and len(ex.spy.calls) == calls


def test_open_ended_window_for_r1_and_r2_without_window_hours(ex, clock):
    r1 = ex.submit(ActionIntent("schedule_follow_up", {"merchant_id": M, "due_at": "2026-11-02T10:00:00+05:30"}, case_id="C-o"))
    t = ex.submit(ActionIntent("raise_ticket", {"merchant_id": M, "type": "DEVICE"}, case_id="C-o"))
    assert r1.compensable_until is None and t.compensable_until is None
    clock.advance(timedelta(days=400))
    assert ex.undo(r1.action_id, AGENT).status == "OK" and ex.undo(t.action_id, AGENT).status == "OK"


def test_undo_goes_through_the_same_pipeline_policy_ledger_breaker(db_path, clock, http, conn):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    out = e.submit(waiver())
    e.policy = DenyAll("TEST-DENY")
    calls = len(spy.calls)
    u = e.undo(out.action_id, AGENT)
    assert u.status == "REFUSED" and [v.rule_id for v in u.violations] == ["TEST-DENY"] and len(spy.calls) == calls
    e.policy = AllowAll()
    u = e.undo(out.action_id, Principal("HUMAN", "priya", "support_executive"))
    assert u.status == "OK"
    es = [x for x in e.ledger.entries("C-1")]
    assert [x.kind for x in es] == ["ACTION_INTENT", "POLICY", "ACTION_RESULT", "POLICY", "COMPENSATION", "ACTION_INTENT", "POLICY", "ACTION_RESULT", "COMPENSATION"]
    comp = [x for x in es if x.kind == "COMPENSATION" and x.payload["status"] == "OK"][0]
    assert comp.actor == "HUMAN:priya" and comp.role == "support_executive" and comp.payload["action"] == "revoke_fee_waiver"
    assert [x.kind for x in es].count("ACTION_INTENT") == 2          # the compensation was ledgered as its own intent


def test_undo_when_the_mock_is_down_leaves_it_open_and_retryable(db_path, clock, http, conn):
    e = make_executor(db_path, clock, SpyHttp(http))
    out = e.submit(waiver())
    chaos(http, "pricing", "error500", 1.0)
    u = e.undo(out.action_id, AGENT)
    assert u.status == "ERROR" and e.compensations.get(out.action_id)["status"] == "OPEN"
    chaos(http, "pricing", "error500", 0)
    assert e.undo(out.action_id, AGENT).status == "OK"
    assert conn.execute("SELECT status FROM offers WHERE waiver_id=?", (out.result["waiver_id"],)).fetchone()[0] == "REVOKED"


def test_undo_unknown_action_is_an_error(ex):
    assert ex.undo("ACT-9999", AGENT).error.startswith("NOT_COMPENSABLE")
    send = ex.submit(ActionIntent("send_message", {"merchant_id": M, "channel": "app", "body": "x"}, case_id="C-1"))
    assert ex.undo(send.action_id, AGENT).error.startswith("NOT_COMPENSABLE")        # R3 is irreversible


def test_undo_still_works_with_the_kill_switch_on(ex):
    out = ex.submit(waiver())
    ex.conn.execute("INSERT INTO meta(key, value) VALUES ('kill_switch','on')")
    assert ex.submit(waiver(seq=1)).status == "REFUSED"
    assert ex.undo(out.action_id, AGENT).status == "OK"          # undoing is risk-reducing, so it is not blocked


def test_preference_undo_restores_the_previous_value_exactly(ex):
    k = "pref:M-0001:lang"
    first = ex.submit(ActionIntent("save_preference", {"merchant_id": M, "key": "lang", "value": "kn"}, case_id="C-p", step_seq=0))
    second = ex.submit(ActionIntent("save_preference", {"merchant_id": M, "key": "lang", "value": "hi"}, case_id="C-p", step_seq=1))
    val = lambda: (ex.conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone() or [None])[0]
    assert val() == "hi" and second.result["previous_value"] == "kn"
    assert ex.undo(second.action_id, AGENT).status == "OK" and val() == "kn"
    assert ex.undo(first.action_id, AGENT).status == "OK" and val() is None          # back to "never set"
    assert ex.spy.calls == []


def test_compensate_pending_r1_undoes_r1_and_hands_r2_to_a_human(ex, conn):
    r1 = ex.submit(ActionIntent("schedule_follow_up", {"merchant_id": M, "due_at": "2026-11-02T10:00:00+05:30"}, case_id="C-d", step_seq=0))
    r1b = ex.submit(ActionIntent("save_preference", {"merchant_id": M, "key": "k", "value": "v"}, case_id="C-d", step_seq=1))
    r2 = ex.submit(ActionIntent("raise_ticket", {"merchant_id": M, "type": "DEVICE"}, case_id="C-d", step_seq=2))
    other = ex.submit(ActionIntent("schedule_follow_up", {"merchant_id": "M-0002", "due_at": "2026-11-02T10:00:00+05:30"}, case_id="C-other"))
    done, for_human = ex.compensate_pending_r1("C-d")
    assert [o.status for o in done] == ["OK", "OK"]
    assert conn.execute("SELECT status FROM schedules WHERE schedule_id=?", (r1.result["schedule_id"],)).fetchone()[0] == "CANCELLED"
    assert conn.execute("SELECT status FROM tickets WHERE ticket_id=?", (r2.result["ticket_id"],)).fetchone()[0] == "OPEN"     # R2 not touched
    assert conn.execute("SELECT status FROM schedules WHERE schedule_id=?", (other.result["schedule_id"],)).fetchone()[0] != "CANCELLED"
    assert for_human == [{"action_id": r2.action_id, "action": "raise_ticket", "compensation": "withdraw_ticket", "compensable_until": for_human[0]["compensable_until"]}]
    assert ex.compensate_pending_r1("C-d")[0] == []                  # idempotent: nothing left
    assert ex.compensations.get(r1b.action_id)["status"] == "DONE"


def test_compensation_actions_are_not_agent_callable_but_run_via_undo(ex):
    assert "revoke_fee_waiver" not in ex.registry.names(agent_only=True)
    out = ex.submit(waiver())
    u = ex.undo(out.action_id, AGENT)
    assert u.status == "OK" and u.action_id != out.action_id
    assert ex.conn.execute("SELECT name FROM actions WHERE action_id=?", (u.action_id,)).fetchone()[0] == "revoke_fee_waiver"
