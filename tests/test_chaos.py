import time

import pytest
from fastapi.testclient import TestClient

from packs.paytm_merchant.mocks import create_app
from packs.paytm_merchant.mocks.client import MockClient, MockServerError, MockTimeout
from tests.conftest import count

TICKET = {"merchant_id": "M-0005", "type": "DEVICE"}


def set_chaos(mock, system, mode, rate, param=None):
    r = mock.http.post("/mock/chaos", json={"system": system, "mode": mode, "rate": rate, "param": param})
    assert r.status_code == 200, r.text


def test_timeout_after_commit_raises_but_the_row_exists(mock, conn):
    set_chaos(mock, "tickets", "timeout_after_commit", 1.0)
    with pytest.raises(MockTimeout):
        mock.write("POST", "/mock/tickets", key="k-1", json=TICKET)
    assert count(conn, "tickets", "merchant_id='M-0005'") == 1  # committed before the timeout
    set_chaos(mock, "tickets", "timeout_after_commit", 0)
    again = mock.write("POST", "/mock/tickets", key="k-1", json=TICKET)  # retry, same key
    assert again.replayed and again.status == 201
    assert count(conn, "tickets", "merchant_id='M-0005'") == 1


def test_error500_happens_before_commit(mock, conn):
    set_chaos(mock, "tickets", "error500", 1.0)
    with pytest.raises(MockServerError):
        mock.write("POST", "/mock/tickets", key="k-1", json=TICKET)
    assert count(conn, "tickets", "merchant_id='M-0005'") == 0
    assert count(conn, "mock_idempotency", "key='k-1'") == 0
    set_chaos(mock, "tickets", "error500", 0)
    ok = mock.write("POST", "/mock/tickets", key="k-1", json=TICKET)
    assert not ok.replayed and count(conn, "tickets", "merchant_id='M-0005'") == 1


def test_error500_also_fails_reads(mock):
    set_chaos(mock, "kyc", "error500", 1.0)
    with pytest.raises(MockServerError):
        mock.get("/mock/kyc/M-0001")
    assert mock.get("/mock/bank/M-0001").status == 200  # other systems unaffected


def test_latency_adds_delay(mock):
    set_chaos(mock, "kyc", "latency", 1.0, 120)
    t0 = time.perf_counter()
    mock.get("/mock/kyc/M-0001")
    assert time.perf_counter() - t0 >= 0.12


def test_duplicate_webhook_is_absorbed_by_event_id_dedupe(mock, conn):
    set_chaos(mock, "payments", "duplicate_webhook", 1.0)
    mock.write("POST", "/mock/payments/simulate", key="k-p", json={"merchant_id": "M-0006", "amount_paise": 5000})
    assert count(conn, "events", "type='PAYMENT_SUCCESS'") == 1
    assert conn.execute("SELECT n FROM counters WHERE name='dup_webhooks_dropped'").fetchone()[0] == 1


def test_wildcard_applies_everywhere_and_specific_rows_win(mock):
    set_chaos(mock, "*", "error500", 1.0)
    with pytest.raises(MockServerError):
        mock.get("/mock/kyc/M-0001")
    with pytest.raises(MockServerError):
        mock.get("/mock/bank/M-0001")
    set_chaos(mock, "kyc", "error500", 0.0001)  # specific row overrides the wildcard for kyc
    assert mock.get("/mock/kyc/M-0001").status == 200


def test_every_mode_is_reproducible_on_any_system(mock, conn):
    for system, path in [("tickets", "/mock/tickets"), ("comms", "/mock/comms/send"), ("pricing", "/mock/offers/waiver")]:
        body = {"tickets": TICKET, "comms": {"merchant_id": "M-0005", "channel": "app", "body": "x"}, "pricing": {"merchant_id": "M-0005", "months": 1}}[system]
        set_chaos(mock, system, "timeout_after_commit", 1.0)
        with pytest.raises(MockTimeout):
            mock.write("POST", path, key=f"k-{system}", json=body)


def test_same_seed_gives_the_same_chaos_sequence(db_path, clock):
    def run(seed):
        app = create_app(db_path, clock, chaos_seed=seed)
        m = MockClient(TestClient(app))
        m.http.post("/mock/chaos", json={"system": "kyc", "mode": "error500", "rate": 0.5})
        out = []
        for _ in range(30):
            try:
                m.get("/mock/kyc/M-0001")
                out.append(0)
            except MockServerError:
                out.append(1)
        return out

    a, b, c = run(5), run(5), run(6)
    assert a == b and a != c and 0 < sum(a) < 30


def test_chaos_validation_and_reset(mock):
    assert mock.http.post("/mock/chaos", json={"system": "nope", "mode": "error500", "rate": 1}).status_code == 422
    assert mock.http.post("/mock/chaos", json={"system": "kyc", "mode": "nope", "rate": 1}).status_code == 422
    set_chaos(mock, "kyc", "error500", 1.0)
    assert len(mock.http.get("/mock/chaos").json()["chaos"]) == 1
    mock.http.post("/mock/chaos/reset")
    assert mock.http.get("/mock/chaos").json()["chaos"] == []
