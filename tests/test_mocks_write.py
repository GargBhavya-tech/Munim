from datetime import timedelta

import pytest

from packs.paytm_merchant.mocks.client import MockClient
from packs.paytm_merchant.mocks.common import MockContext, Result, execute_write
from tests.conftest import count

WAIVER = {"merchant_id": "M-0042", "offer_id": "FEE_WAIVER_1M", "months": 1}


def post(mock, path, key, body=None, method="POST"):
    return mock.write(method, path, key=key, json=body)


def test_same_key_twice_gives_one_row_and_identical_bodies(mock, conn):
    before = count(conn, "offers")
    a = post(mock, "/mock/offers/waiver", "k-1", WAIVER)
    b = post(mock, "/mock/offers/waiver", "k-1", WAIVER)
    assert a.body == b.body and a.status == b.status == 201
    assert not a.replayed and b.replayed
    assert count(conn, "offers") == before + 1


def test_different_key_creates_a_second_row(mock, conn):
    before = count(conn, "offers")
    a = post(mock, "/mock/offers/waiver", "k-1", WAIVER)
    b = post(mock, "/mock/offers/waiver", "k-2", WAIVER)
    assert a.body["waiver_id"] != b.body["waiver_id"]
    assert count(conn, "offers") == before + 2


def test_missing_idempotency_key_is_rejected(http, conn):
    r = http.post("/mock/offers/waiver", json=WAIVER)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert count(conn, "offers") == 0
    assert http.delete("/mock/offers/waiver/W-0001").status_code == 400  # compensations need keys too


def test_same_key_with_different_request_is_refused(mock, conn):
    post(mock, "/mock/offers/waiver", "k-1", WAIVER)
    r = post(mock, "/mock/offers/waiver", "k-1", {**WAIVER, "months": 6})
    assert r.status == 422 and r.body["error"] == "IDEMPOTENCY_KEY_REUSED"
    assert count(conn, "offers") == 1


def test_key_and_result_commit_together_so_a_crash_leaves_neither(db_path, clock, conn):
    ctx = MockContext(db_path, clock)

    def crashing(c):
        c.execute("INSERT INTO tickets VALUES ('T-X','M-0042','OTHER','OPEN',NULL,NULL,'t',NULL)")
        raise RuntimeError("crash between write and key store")

    with pytest.raises(RuntimeError):
        execute_write(ctx, "tickets", "k-crash", {"op": "x"}, crashing)
    assert count(conn, "tickets", "ticket_id='T-X'") == 0
    assert count(conn, "mock_idempotency", "key='k-crash'") == 0
    ok = execute_write(ctx, "tickets", "k-crash", {"op": "x"}, lambda c: Result(200, {"fine": True}))
    assert ok.status_code == 200


def test_business_errors_are_stored_and_replayed_without_partial_writes(mock, conn):
    a = post(mock, "/mock/devices/M-0007/replacement", "k-nodev", {"reason": "X"})  # M-0007 has no device
    b = post(mock, "/mock/devices/M-0007/replacement", "k-nodev", {"reason": "X"})
    assert a.status == 409 and a.body == b.body and b.replayed
    assert count(conn, "device_replacements", "merchant_id='M-0007'") == 0


def test_replacement_create_cancel_and_window(mock, conn, clock):
    r = post(mock, "/mock/devices/M-0005/replacement", "k-r", {"reason": "NO_ANNOUNCEMENT"})
    rid = r.body["replacement_id"]
    assert r.status == 201 and r.body["status"] == "CREATED"
    c1 = post(mock, f"/mock/devices/M-0005/replacement/{rid}", "k-c1", method="DELETE")
    c2 = post(mock, f"/mock/devices/M-0005/replacement/{rid}", "k-c2", method="DELETE")  # different key: still a safe no-op
    assert c1.body["status"] == c2.body["status"] == "CANCELLED"
    r2 = post(mock, "/mock/devices/M-0005/replacement", "k-r2", {"reason": "X"})
    clock.advance(timedelta(hours=25))
    late = post(mock, f"/mock/devices/M-0005/replacement/{r2.body['replacement_id']}", "k-late", method="DELETE")
    assert late.status == 409 and late.body["error"] == "WINDOW_CLOSED"


def test_waiver_revoke_restores_and_expires(mock, conn, clock):
    w = post(mock, "/mock/offers/waiver", "k-w", WAIVER).body["waiver_id"]
    assert post(mock, f"/mock/offers/waiver/{w}", "k-u1", method="DELETE").body["status"] == "REVOKED"
    assert post(mock, f"/mock/offers/waiver/{w}", "k-u2", method="DELETE").body["status"] == "REVOKED"
    w2 = post(mock, "/mock/offers/waiver", "k-w2", WAIVER).body["waiver_id"]
    clock.advance(timedelta(days=8))
    assert post(mock, f"/mock/offers/waiver/{w2}", "k-u3", method="DELETE").status == 409


def test_payment_simulate_creates_settlement_and_one_event(mock, conn):
    a = post(mock, "/mock/payments/simulate", "k-p1", {"merchant_id": "M-0006", "amount_paise": 24000})
    b = post(mock, "/mock/payments/simulate", "k-p2", {"merchant_id": "M-0006", "amount_paise": 10000})
    assert a.body["settlement_id"] == b.body["settlement_id"]  # same pending settlement
    s = conn.execute("SELECT amount_paise, status FROM settlements WHERE settlement_id=?", (a.body["settlement_id"],)).fetchone()
    assert s["amount_paise"] == 34000 and s["status"] == "PENDING"
    assert count(conn, "events", "type='PAYMENT_SUCCESS' AND merchant_id='M-0006'") == 2


def test_settlement_advance_emits_event_and_rejects_bad_transitions(mock, conn):
    sid = post(mock, "/mock/payments/simulate", "k-p", {"merchant_id": "M-0006", "amount_paise": 100}).body["settlement_id"]
    ok = post(mock, f"/mock/settlements/{sid}/advance", "k-a", {"to": "SETTLED"})
    assert ok.body["status"] == "SETTLED"
    assert count(conn, "events", "type='SETTLEMENT_SETTLED'") == 1
    again = post(mock, f"/mock/settlements/{sid}/advance", "k-a2", {"to": "HELD_RISK"})
    assert again.status == 409


def test_refund_waits_for_merchant_confirmation_in_app(mock, conn):
    pay = mock.get("/mock/payments", mid="M-0004", limit=1).body["payments"][0]
    r = post(mock, "/mock/refunds", "k-rf", {"merchant_id": "M-0004", "payment_id": pay["payment_id"], "amount_paise": pay["amount_paise"]})
    assert r.body["status"] == "AWAITING_MERCHANT_CONFIRM"
    assert conn.execute("SELECT status FROM refunds").fetchone()[0] == "AWAITING_MERCHANT_CONFIRM"
    done = post(mock, f"/mock/refunds/{r.body['refund_id']}/merchant-confirm", "k-conf")
    assert done.body["status"] == "PROCESSED"
    assert post(mock, f"/mock/refunds/{r.body['refund_id']}", "k-x", method="DELETE").status == 409  # too late to cancel


def test_refund_cannot_exceed_original_payment(mock):
    pay = mock.get("/mock/payments", mid="M-0004", limit=1).body["payments"][0]
    r = post(mock, "/mock/refunds", "k-big", {"merchant_id": "M-0004", "payment_id": pay["payment_id"], "amount_paise": pay["amount_paise"] + 1})
    assert r.status == 422 and r.body["error"] == "REFUND_EXCEEDS_PAYMENT"


def test_loan_link_only_for_eligible_merchants(mock, conn):
    ok = post(mock, "/mock/lender/application-link", "k-l1", {"merchant_id": "M-0003"})
    assert ok.status == 201 and "Key Fact Statement" in ok.body["kfs_note"]
    no = post(mock, "/mock/lender/application-link", "k-l2", {"merchant_id": "M-0001"})
    assert no.status == 409 and no.body["error"] == "NOT_ELIGIBLE"
    assert count(conn, "lender_apps") == 1


def test_bank_retrigger_verifies_transient_failures_only(mock, conn):
    r = post(mock, "/mock/bank/M-0001/retrigger", "k-b")
    assert r.body["status"] == "VERIFIED" and r.body["attempts"] == 2
    conn.execute("UPDATE bank SET status='FAILED', reason='NAME_MISMATCH' WHERE merchant_id='M-0004'")
    assert post(mock, "/mock/bank/M-0004/retrigger", "k-b2").body["status"] == "FAILED"


def test_kyc_link_refused_when_already_approved(mock):
    assert post(mock, "/mock/kyc/M-0004/resume-link", "k-k").status == 409
    ok = post(mock, "/mock/kyc/M-0001/resume-link", "k-k2")
    assert ok.status == 200 and ok.body["url"].startswith("https://pay.paytm.invalid/")


def test_plan_change_and_revert_before_billing(mock, conn):
    c = post(mock, "/mock/plans/M-0004", "k-pc", {"to_plan_id": "SOUNDBOX_PLUS"})
    assert c.status == 201
    assert conn.execute("SELECT pending_plan_id FROM plans WHERE merchant_id='M-0004'").fetchone()[0] == "SOUNDBOX_PLUS"
    r = post(mock, f"/mock/plans/M-0004/change/{c.body['change_id']}", "k-rv", method="DELETE")
    assert r.body["status"] == "REVERTED"
    assert conn.execute("SELECT pending_plan_id FROM plans WHERE merchant_id='M-0004'").fetchone()[0] is None


def test_ticket_schedule_booking_comms_closure_roundtrip(mock, conn):
    t = post(mock, "/mock/tickets", "k-t", {"merchant_id": "M-0005", "type": "DEVICE", "system_object": "D-0001"}).body["ticket_id"]
    assert post(mock, f"/mock/tickets/{t}", "k-tw", method="DELETE").body["status"] == "WITHDRAWN"
    s = post(mock, "/mock/schedule", "k-s", {"merchant_id": "M-0005", "due_at": "2026-11-01T10:00:00+05:30", "note": "check"}).body["schedule_id"]
    assert post(mock, f"/mock/schedule/{s}", "k-sc", method="DELETE").body["status"] == "CANCELLED"
    b = post(mock, "/mock/bookings", "k-b", {"merchant_id": "M-0005", "kind": "visit", "slot_at": "2026-11-01T15:00:00+05:30"}).body["booking_id"]
    assert post(mock, f"/mock/bookings/{b}", "k-bc", method="DELETE").body["status"] == "CANCELLED"
    assert post(mock, "/mock/comms/send", "k-m", {"merchant_id": "M-0005", "channel": "app", "body": "hello"}).status == 201
    assert post(mock, "/mock/comms/send", "k-m2", {"merchant_id": "M-0005", "channel": "pigeon", "body": "x"}).status == 422
    c = post(mock, "/mock/closures", "k-cl", {"merchant_id": "M-0005", "reason": "moving"}).body["closure_id"]
    assert post(mock, f"/mock/closures/{c}", "k-clc", method="DELETE").body["status"] == "CANCELLED"
    assert count(conn, "comms_outbox") == 1


def test_ids_continue_from_seeded_counters(mock):
    t = post(mock, "/mock/tickets", "k-t", {"merchant_id": "M-0005", "type": "OTHER"}).body["ticket_id"]
    assert t == "T-0002"  # one ticket was seeded for M-0002
