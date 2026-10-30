"""GATE 1: exactly-once under chaos. If this fails, every 'takes actions' claim is false.

200 randomised logical writes across the mock systems, with ``timeout_after_commit`` at 30% and
``duplicate_webhook`` at 20% on every system. The client retries timeouts with the SAME key (a fresh
key per retry would be the bug this gate exists to catch). Afterwards every logical write must have
produced exactly one row, nothing may be orphaned, events must be deduplicated and the ledger must verify.
"""
import random
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from core.audit.ledger import Ledger, verify_chain
from core.engine.clock import SimClock
from packs.paytm_merchant.mocks import create_app
from packs.paytm_merchant.mocks.client import MockClient

N_WRITES = 200

# kind -> (table, id column in that table, key in the response body)
ENTITIES = {
    "waiver": ("offers", "waiver_id", "waiver_id"),
    "ticket": ("tickets", "ticket_id", "ticket_id"),
    "schedule": ("schedules", "schedule_id", "schedule_id"),
    "booking": ("bookings", "booking_id", "booking_id"),
    "comms": ("comms_outbox", "comm_id", "comm_id"),
    "replacement": ("device_replacements", "replacement_id", "replacement_id"),
    "order": ("device_orders", "order_id", "order_id"),
    "closure": ("closures", "closure_id", "closure_id"),
    "plan": ("plan_changes", "change_id", "change_id"),
    "payment": ("payments", "payment_id", "payment_id"),
    "refund": ("refunds", "refund_id", "refund_id"),
    "kyc_link": ("links", "link_id", "link_id"),
    "lender": ("lender_apps", "app_id", "app_id"),
}
FINAL = {"waiver": "REVOKED", "ticket": "WITHDRAWN", "schedule": "CANCELLED", "booking": "CANCELLED",
         "replacement": "CANCELLED", "order": "CANCELLED", "closure": "CANCELLED", "refund": "CANCELLED", "plan": "REVERTED"}
STATUS_COL = {"offers": "status", "tickets": "status", "schedules": "status", "bookings": "status", "device_replacements": "status",
              "device_orders": "status", "closures": "status", "refunds": "status", "plan_changes": "status"}


def cancel_path(kind, mid, eid):
    return {
        "waiver": f"/mock/offers/waiver/{eid}", "ticket": f"/mock/tickets/{eid}", "schedule": f"/mock/schedule/{eid}",
        "booking": f"/mock/bookings/{eid}", "replacement": f"/mock/devices/{mid}/replacement/{eid}",
        "order": f"/mock/devices/{mid}/order/{eid}", "closure": f"/mock/closures/{eid}", "refund": f"/mock/refunds/{eid}",
        "plan": f"/mock/plans/{mid}/change/{eid}",
    }[kind]


def run_gate(db_path, chaos_modes, seed=2026):
    rng = random.Random(seed)
    clock = SimClock()
    app = create_app(db_path, clock, chaos_seed=seed)
    http = TestClient(app)
    client = MockClient(http)
    for mode, rate in chaos_modes.items():
        assert http.post("/mock/chaos", json={"system": "*", "mode": mode, "rate": rate}).status_code == 200

    from core.memory.store import connect
    from packs.paytm_merchant.mocks.client import MockServerError

    def get(path, **params):
        """Reads are subject to chaos too; they are safe to retry."""
        for _ in range(60):
            try:
                return client.get(path, **params)
            except MockServerError:
                continue
        raise RuntimeError("read never succeeded")

    conn = connect(db_path)
    ledger = Ledger(conn, clock)
    baseline = {k: {r[0] for r in conn.execute(f"SELECT {col} FROM {t}")} for k, (t, col, _) in ENTITIES.items()}
    baseline_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    device_merchants = [f"M-{i:04d}" for i in (1, 2, 3, 4, 5, 6, 10, 11, 12)]
    any_merchants = [f"M-{i:04d}" for i in range(1, 41)]
    plan_pool = [f"M-{i:04d}" for i in range(41, 120)]
    refund_payments = [(r["payment_id"], r["merchant_id"], r["amount_paise"]) for r in conn.execute(
        "SELECT payment_id, merchant_id, amount_paise FROM payments WHERE status='SUCCESS' AND merchant_id IN ('M-0004','M-0003','M-0007') LIMIT 60")]
    rng.shuffle(plan_pool)
    rng.shuffle(refund_payments)
    eligible = [m for m in ("M-0003", "M-0004", "M-0007", "M-0011") if get("/mock/lender/eligibility", mid=m).body["eligible"]]
    lender_pool = list(eligible)

    created: dict[str, list[str]] = {k: [] for k in ENTITIES}          # ids returned to the caller
    cancelable: list[tuple[str, str, str]] = []                         # (kind, merchant, id)
    cancelled: list[tuple[str, str]] = []
    pending_settlements: list[str] = []
    advanced: list[str] = []
    logical = []
    attempts_total = 0
    replayed_resends = 0

    def do(kind, method, path, body):
        nonlocal attempts_total, replayed_resends
        key = f"gate-{len(logical):04d}"
        ledger.append(kind="ACTION_INTENT", actor="ENGINE", case_id="C-GATE", payload={"key": key, "kind": kind, "path": path})
        resp, attempts = client.write_with_retry(method, path, key=key, json=body, attempts=60)
        attempts_total += attempts
        assert resp.status < 400, (kind, path, resp.status, resp.body)
        if rng.random() < 0.3:  # a duplicate send of the same logical write must be a pure replay
            again, n = client.write_with_retry(method, path, key=key, json=body, attempts=60)
            attempts_total += n
            assert again.replayed and again.body == resp.body
            replayed_resends += 1
        ledger.append(kind="ACTION_RESULT", actor="ENGINE", case_id="C-GATE", payload={"key": key, "status": resp.status})
        logical.append(key)
        return resp

    while len(logical) < N_WRITES:
        roll = rng.random()
        if roll < 0.28 and cancelable:
            kind, mid, eid = cancelable.pop(rng.randrange(len(cancelable)))
            do("cancel:" + kind, "DELETE", cancel_path(kind, mid, eid), None)
            cancelled.append((kind, eid))
            continue
        if roll < 0.36 and pending_settlements:
            sid = pending_settlements.pop(rng.randrange(len(pending_settlements)))
            do("advance", "POST", f"/mock/settlements/{sid}/advance", {"to": "SETTLED"})
            advanced.append(sid)
            continue
        kind = rng.choice(["waiver", "ticket", "schedule", "booking", "comms", "replacement", "order", "closure", "plan",
                           "payment", "payment", "refund", "kyc_link", "lender"])
        mid = rng.choice(any_merchants)
        if kind == "waiver":
            r = do(kind, "POST", "/mock/offers/waiver", {"merchant_id": mid, "months": rng.randint(1, 3)})
        elif kind == "ticket":
            r = do(kind, "POST", "/mock/tickets", {"merchant_id": mid, "type": rng.choice(["DEVICE", "SETTLEMENT", "OTHER"])})
        elif kind == "schedule":
            r = do(kind, "POST", "/mock/schedule", {"merchant_id": mid, "due_at": "2026-11-01T10:00:00+05:30"})
        elif kind == "booking":
            r = do(kind, "POST", "/mock/bookings", {"merchant_id": mid, "kind": "visit", "slot_at": "2026-11-01T15:00:00+05:30"})
        elif kind == "comms":
            r = do(kind, "POST", "/mock/comms/send", {"merchant_id": mid, "channel": "app", "body": "hello"})
        elif kind == "replacement":
            mid = rng.choice(device_merchants)
            r = do(kind, "POST", f"/mock/devices/{mid}/replacement", {"reason": "NO_ANNOUNCEMENT"})
        elif kind == "order":
            r = do(kind, "POST", f"/mock/devices/{mid}/order", {"model": "Soundbox 4G"})
        elif kind == "closure":
            r = do(kind, "POST", "/mock/closures", {"merchant_id": mid, "reason": "test"})
        elif kind == "plan":
            if not plan_pool:
                continue
            mid = plan_pool.pop()
            current = get(f"/mock/plans/{mid}").body["plan_id"]
            to = "SOUNDBOX_PLUS" if current != "SOUNDBOX_PLUS" else "BASIC"
            r = do(kind, "POST", f"/mock/plans/{mid}", {"to_plan_id": to})
        elif kind == "payment":
            mid = rng.choice(any_merchants)
            r = do(kind, "POST", "/mock/payments/simulate", {"merchant_id": mid, "amount_paise": rng.randint(1, 500) * 100})
            if r.body["settlement_id"] not in pending_settlements:
                pending_settlements.append(r.body["settlement_id"])
        elif kind == "refund":
            if not refund_payments:
                continue
            pid, mid, amt = refund_payments.pop()
            r = do(kind, "POST", "/mock/refunds", {"merchant_id": mid, "payment_id": pid, "amount_paise": amt})
        elif kind == "kyc_link":
            r = do(kind, "POST", "/mock/bank/%s/update-link" % mid, None)
        elif kind == "lender":
            if not lender_pool:
                continue
            mid = lender_pool.pop()
            r = do(kind, "POST", "/mock/lender/application-link", {"merchant_id": mid})
        created[kind].append(r.body[ENTITIES[kind][2]])
        if kind in FINAL:
            cancelable.append((kind, mid, r.body[ENTITIES[kind][2]]))

    # a settlement can be advanced only once; payments that joined it later changed nothing about that
    verdict = verify_chain(conn)
    stats = Counter()
    problems = []

    # 1. every logical create produced exactly one row, and no other new rows exist (no orphans, no duplicates)
    for kind, (table, col, _) in ENTITIES.items():
        now_ids = [r[0] for r in conn.execute(f"SELECT {col} FROM {table}")]
        assert len(now_ids) == len(set(now_ids)), f"duplicate primary keys in {table}"
        new_ids = set(now_ids) - baseline[kind]
        if new_ids != set(created[kind]):
            problems.append(f"{table}: rows {sorted(new_ids)[:5]} != responses {sorted(created[kind])[:5]}")
        if len(created[kind]) != len(set(created[kind])):
            problems.append(f"{table}: a create returned the same id twice")
        stats[table] = len(new_ids)

    # 2. cancellations landed exactly once on their targets
    for kind, eid in cancelled:
        table, col, _ = ENTITIES[kind]
        st = conn.execute(f"SELECT {STATUS_COL[table]} FROM {table} WHERE {col} = ?", (eid,)).fetchone()[0]
        if st != FINAL[kind]:
            problems.append(f"{kind} {eid} is {st}, expected {FINAL[kind]}")

    # 3. idempotency store: one row per logical write, no more
    n_keys = conn.execute("SELECT COUNT(*) FROM mock_idempotency").fetchone()[0]
    if n_keys != len(logical):
        problems.append(f"mock_idempotency has {n_keys} rows for {len(logical)} logical writes")

    # 4. events: duplicate webhooks were really delivered and really deduplicated
    new_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] - baseline_events
    expected_events = len(created["payment"]) + len(advanced)
    if new_events != expected_events:
        problems.append(f"events: {new_events} rows for {expected_events} logical events")
    dropped = conn.execute("SELECT n FROM counters WHERE name='dup_webhooks_dropped'").fetchone()
    dropped = dropped[0] if dropped else 0

    # 5. ledger: one INTENT + one RESULT per logical write, chain intact
    if verdict.entries != 2 * len(logical):
        problems.append(f"ledger has {verdict.entries} entries for {len(logical)} logical writes")
    return dict(problems=problems, verdict=verdict, logical=len(logical), attempts=attempts_total, dropped=dropped,
                replayed_resends=replayed_resends, stats=stats, new_events=new_events)


def test_gate_1_exactly_once_200_of_200(db_path):
    out = run_gate(db_path, {"timeout_after_commit": 0.30, "duplicate_webhook": 0.20})
    assert out["problems"] == []
    assert out["verdict"].ok
    assert out["logical"] == N_WRITES
    # the chaos really happened: many more attempts than writes, and duplicate webhooks were dropped
    assert out["attempts"] > N_WRITES * 1.25, out["attempts"]
    assert out["dropped"] > 0 and out["new_events"] > 0
    assert out["replayed_resends"] > 20


def test_gate_1_also_holds_with_error500_before_commit(db_path):
    out = run_gate(db_path, {"timeout_after_commit": 0.30, "duplicate_webhook": 0.20, "error500": 0.20}, seed=7)
    assert out["problems"] == []
    assert out["verdict"].ok and out["logical"] == N_WRITES


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_gate_1_holds_across_seeds(db_path, seed):
    out = run_gate(db_path, {"timeout_after_commit": 0.30, "duplicate_webhook": 0.20}, seed=seed)
    assert out["problems"] == [] and out["verdict"].ok


def test_gate_1_is_not_vacuous_it_catches_a_fresh_key_per_retry(db_path, monkeypatch):
    """Mutation check: the classic bug (new idempotency key on every retry) must make the gate fail."""
    from packs.paytm_merchant.mocks.client import MockServerError, MockTimeout

    def buggy(self, method, path, *, key, json=None, attempts=30):
        for i in range(1, attempts + 1):
            try:
                return self.write(method, path, key=f"{key}-attempt{i}", json=json), i
            except (MockTimeout, MockServerError):
                continue
        raise RuntimeError("gave up")

    monkeypatch.setattr(MockClient, "write_with_retry", buggy)
    try:
        out = run_gate(db_path, {"timeout_after_commit": 0.30, "duplicate_webhook": 0.20})
    except AssertionError:
        return  # caught by an inline assertion
    assert out["problems"], "gate failed to detect duplicated writes"
