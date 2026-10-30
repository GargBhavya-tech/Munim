"""#26: R3 sends are queued in the same transaction as the intent and delivered exactly once after commit."""
import pytest

from core.actions.types import ActionIntent
from tests.test_executor import M, Crash, SpyHttp, kinds, make_executor, n

SEND = dict(action="send_message", args={"merchant_id": M, "channel": "app", "body": "ನಮಸ್ಕಾರ", "language": "kn"})


def send(case="C-s", seq=0):
    return ActionIntent(SEND["action"], SEND["args"], case_id=case, step_seq=seq)


def test_send_writes_outbox_row_then_delivers_and_marks_sent(db_path, clock, http, conn):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    spy.conn = e.conn
    out = e.submit(send())
    assert out.status == "OK" and out.result["status"] == "SENT"
    row = e.outbox.get(out.action_id)
    assert row["status"] == "SENT" and row["idempotency_key"] == out.idempotency_key and row["attempts"] == 1 and row["sent_at"]
    assert n(conn, "comms_outbox", "comm_id = ? AND status = 'SENT'", (out.result["comm_id"],)) == 1
    assert kinds(e, "C-s") == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]
    assert spy.in_txn_at_call == [False]               # delivered AFTER commit, never inside the transaction


def test_all_r3_actions_use_the_outbox_and_reads_and_r1_do_not(db_path, clock, http):
    e = make_executor(db_path, clock, SpyHttp(http))
    e.submit(send())
    e.submit(ActionIntent("send_kyc_resume_link", {"merchant_id": M}, case_id="C-s2"))
    e.submit(ActionIntent("raise_ticket", {"merchant_id": M, "type": "DEVICE"}, case_id="C-s3"))
    e.submit(ActionIntent("get_offer_band", {"merchant_id": M}, case_id="C-s4"))
    assert [r[0] for r in e.conn.execute("SELECT a.name FROM outbox o JOIN actions a USING(action_id) ORDER BY o.outbox_id")] == ["send_message", "send_kyc_resume_link"]


def test_crash_after_commit_before_delivery_restart_delivers_exactly_once(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.crash_before = 1
    e1 = make_executor(db_path, clock, spy)
    base = n(conn, "comms_outbox")
    with pytest.raises(Crash):
        e1.submit(send())
    assert n(conn, "comms_outbox") == base                                     # nothing sent
    row = e1.outbox.pending()
    assert len(row) == 1 and row[0]["status"] == "PENDING"                    # but recorded: not lost
    assert kinds(e1, "C-s") == ["ACTION_INTENT", "POLICY"]
    e1.conn.close()

    spy2 = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy2)
    outs = e2.deliver_pending()
    assert [o.status for o in outs] == ["OK"] and n(conn, "comms_outbox") == base + 1
    assert e2.outbox.pending() == [] and kinds(e2, "C-s") == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]
    assert e2.deliver_pending() == [] and e2.deliver_pending() == []           # again and again: nothing
    assert len(spy2.writes()) == 1 and n(conn, "comms_outbox") == base + 1


def test_crash_after_delivery_before_mark_sent_is_safe_because_the_key_is_reused(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.crash_after = 1                                                         # the mock committed, then the process died
    e1 = make_executor(db_path, clock, spy)
    base = n(conn, "comms_outbox")
    with pytest.raises(Crash):
        e1.submit(send())
    assert n(conn, "comms_outbox") == base + 1 and e1.outbox.pending()[0]["status"] == "PENDING"
    e1.conn.close()
    spy2 = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy2)
    outs = e2.deliver_pending()
    assert len(outs) == 1 and outs[0].status == "OK"
    assert n(conn, "comms_outbox") == base + 1                                  # no duplicate message to the merchant
    assert spy2.writes()[0][2] == outs[0].idempotency_key
    assert e2.outbox.pending() == []


def test_resubmitting_the_intent_after_a_crash_does_not_double_queue(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.crash_before = 1
    e1 = make_executor(db_path, clock, spy)
    with pytest.raises(Crash):
        e1.submit(send())
    e1.conn.close()
    e2 = make_executor(db_path, clock, SpyHttp(http))
    base = n(conn, "comms_outbox")
    assert e2.submit(send()).status == "OK"
    assert n(e2.conn, "outbox") == 1 and n(conn, "comms_outbox") == base + 1 and e2.deliver_pending() == []


def test_rollback_leaves_no_outbox_row_and_nothing_sent(db_path, clock, http, conn, monkeypatch):
    """Never send inside a transaction that can roll back: a failure while queuing sends nothing and queues nothing."""
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    real = e.ledger.append

    def boom(**kw):
        if kw["kind"] == "ACTION_INTENT":
            raise OSError("disk full")
        return real(**kw)

    monkeypatch.setattr(e.ledger, "append", boom)
    base = n(conn, "comms_outbox")
    with pytest.raises(OSError):
        e.submit(send())
    assert spy.calls == [] and n(conn, "comms_outbox") == base
    assert n(e.conn, "outbox") == 0 and n(e.conn, "actions") == 0 and n(e.conn, "idempotency") == 0


def test_kill_switch_holds_already_queued_rows(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.crash_before = 1
    e1 = make_executor(db_path, clock, spy)
    with pytest.raises(Crash):
        e1.submit(send())
    e1.conn.execute("INSERT INTO meta(key, value) VALUES ('kill_switch','on')")
    e1.conn.close()

    spy2 = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy2)
    base = n(conn, "comms_outbox")
    assert e2.deliver_pending() == [] and e2.outbox.deliver_pending() == []
    assert e2.recover() == []
    assert spy2.calls == [] and n(conn, "comms_outbox") == base and len(e2.outbox.pending()) == 1
    assert e2.submit(send()).status == "REFUSED"                               # re-submit is held too, not sent behind the switch
    assert spy2.calls == []
    e2.conn.execute("UPDATE meta SET value='off' WHERE key='kill_switch'")
    assert [o.status for o in e2.deliver_pending()] == ["OK"] and n(conn, "comms_outbox") == base + 1


def test_failed_delivery_stays_queued_and_a_later_sweep_delivers_once(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.fail_with = RuntimeError("network down")
    e = make_executor(db_path, clock, spy)
    base = n(conn, "comms_outbox")
    out = e.submit(send())
    assert out.status == "ERROR" and "UPSTREAM_UNAVAILABLE" in out.error and len(e.outbox.pending()) == 1
    spy.fail_with = None
    assert [o.status for o in e.deliver_pending()] == ["OK"] and n(conn, "comms_outbox") == base + 1
    assert e.outbox.get(out.action_id)["attempts"] == 2 and e.deliver_pending() == []


def test_business_rejection_closes_the_outbox_row(db_path, clock, http, conn):
    e = make_executor(db_path, clock, SpyHttp(http))
    out = e.submit(ActionIntent("send_message", {"merchant_id": M, "channel": "carrier_pigeon", "body": "x"}, case_id="C-b"))
    assert out.status == "ERROR" and out.error == "UNKNOWN_CHANNEL"
    assert e.outbox.get(out.action_id)["status"] == "FAILED" and e.deliver_pending() == []
