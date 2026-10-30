"""#25 (+ #28 retry/breaker integration): the executor pipeline. Also home of the shared test doubles."""
import json
import re
import uuid
from pathlib import Path

import pytest

from core.actions.executor import Executor, ReadError
from core.actions.types import AGENT, ActionIntent, PolicyDecision, Principal, Violation
from core.audit.ledger import Ledger, verify_chain
from core.memory.store import connect
from packs.paytm_merchant.actions import build_registry

ROOT = Path(__file__).resolve().parent.parent
M = "M-0001"


# ------------------------------------------------------------------------------------------ doubles
class AllowAll:
    policy_hash = "test-policy"

    def evaluate(self, intent, reader=None):
        return PolicyDecision("ALLOW", (), self.policy_hash)


class DenyAll:
    def __init__(self, rule_id="TEST-DENY"):
        self.rule_id, self.policy_hash = rule_id, "test-policy"

    def evaluate(self, intent, reader=None):
        return PolicyDecision("BLOCK", (Violation(self.rule_id, "t", "BLOCK", "denied for test", action=intent.action),), self.policy_hash)


class EscalateAll:
    policy_hash = "test-policy"

    def evaluate(self, intent, reader=None):
        return PolicyDecision("ESCALATE", (Violation("TEST-ESC", "t", "ESCALATE", "needs a human", action=intent.action, route="risk_reviewer"),), self.policy_hash)


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so nothing in the executor may swallow it)."""


class SpyHttp:
    """Wraps the real TestClient; counts calls, can crash before/after the real call, can swap keys."""

    def __init__(self, http, conn=None):
        self.http, self.conn = http, conn
        self.calls: list[tuple[str, str, str | None]] = []
        self.crash_before = self.crash_after = 0     # crash on the next N calls
        self.fail_with: Exception | None = None
        self.fresh_keys = False
        self.in_txn_at_call: list[bool] = []

    def request(self, method, url, **kw):
        headers = dict(kw.get("headers") or {})
        if self.fresh_keys and "Idempotency-Key" in headers:
            headers["Idempotency-Key"] = uuid.uuid4().hex
            kw["headers"] = headers
        self.calls.append((method, url, headers.get("Idempotency-Key")))
        if self.conn is not None:
            self.in_txn_at_call.append(self.conn.in_transaction)
        if self.fail_with:
            raise self.fail_with
        if self.crash_before:
            self.crash_before -= 1
            raise Crash("before")
        r = self.http.request(method, url, **kw)
        if self.crash_after:
            self.crash_after -= 1
            raise Crash("after")
        return r

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def make_executor(db_path, clock, http, policy=None, sleep=lambda s: None, **kw):
    conn = connect(db_path)
    return Executor(conn, clock, Ledger(conn, clock), build_registry(), http, policy or AllowAll(), sleep=sleep, **kw)


def waiver(case="C-1", seq=0, months=1, merchant=M, principal=AGENT):
    return ActionIntent("apply_fee_waiver", {"merchant_id": merchant, "months": months}, case_id=case, step_seq=seq, principal=principal)


def n(conn, table, where="1=1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def kinds(ex, case="C-1"):
    return [e.kind for e in ex.ledger.entries(case)]


@pytest.fixture
def spy(http, conn):
    return SpyHttp(http, conn)


@pytest.fixture
def ex(db_path, clock, spy):
    e = make_executor(db_path, clock, spy)
    spy.conn = e.conn
    yield e
    e.conn.close()


# ------------------------------------------------------------------------------------------ pipeline
def test_successful_write_ledgers_intent_policy_result_in_order(ex, conn):
    before = n(conn, "offers")
    out = ex.submit(waiver())
    assert out.status == "OK" and out.result["status"] == "APPLIED" and out.action_id == "ACT-0001"
    assert n(conn, "offers") == before + 1
    entries = ex.ledger.entries("C-1")
    assert [e.kind for e in entries] == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]
    assert entries[0].seq < entries[1].seq < entries[2].seq
    assert entries[0].payload["action"] == "apply_fee_waiver" and entries[0].payload["key"] == out.idempotency_key
    assert entries[1].payload["allow"] is True and entries[1].policy_hash == "test-policy"
    assert entries[2].payload["status"] == "OK" and entries[2].payload["key"] == out.idempotency_key
    assert entries[0].actor == "AGENT"
    assert verify_chain(ex.conn).ok


def test_every_registry_write_class_goes_through_the_same_three_entries(ex):
    for i, (act, args) in enumerate([
        ("schedule_follow_up", {"merchant_id": M, "due_at": "2026-11-02T10:00:00+05:30"}),
        ("raise_ticket", {"merchant_id": M, "type": "DEVICE"}),
        ("send_message", {"merchant_id": M, "channel": "app", "body": "hello"}),
        ("save_preference", {"merchant_id": M, "key": "lang", "value": "kn"}),
    ]):
        case = f"C-w{i}"
        out = ex.submit(ActionIntent(act, args, case_id=case))
        assert out.status == "OK", (act, out.error)
        assert kinds(ex, case) == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"], act


def test_ledger_has_intent_before_the_mock_is_called(ex, spy):
    seen = {}
    real = spy.request

    def probing(method, url, **kw):
        seen["kinds_at_call"] = kinds(ex)
        seen["in_txn"] = ex.conn.in_transaction
        return real(method, url, **kw)

    spy.request = probing
    assert ex.submit(waiver()).status == "OK"
    assert seen["kinds_at_call"] == ["ACTION_INTENT", "POLICY"]     # intent + policy committed, result not yet
    assert seen["in_txn"] is False                                  # and nothing is held open across the network call


def test_idempotency_header_is_sent_and_equals_the_deterministic_key(ex, spy):
    out = ex.submit(waiver())
    assert spy.writes() == [("POST", "/mock/offers/waiver", out.idempotency_key)]
    from core.actions.idempotency import make_key
    assert out.idempotency_key == make_key("C-1", "apply_fee_waiver", {"merchant_id": M, "months": 1, "offer_id": "FEE_WAIVER_1M"}, 0)


def test_duplicate_submit_is_replayed_with_one_row_and_one_mock_call(ex, spy, conn):
    base = n(conn, "offers")
    a = ex.submit(waiver())
    b = ex.submit(waiver())
    assert (a.status, b.status) == ("OK", "REPLAYED")
    assert b.result == a.result and b.idempotency_key == a.idempotency_key and b.action_id == a.action_id
    assert n(conn, "offers") == base + 1 and len(spy.writes()) == 1
    assert n(ex.conn, "actions") == 1 and kinds(ex) == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]


def test_replay_survives_a_restart_without_resubmitting(db_path, clock, http, conn):
    spy1 = SpyHttp(http)
    e1 = make_executor(db_path, clock, spy1)
    a = e1.submit(waiver())
    e1.conn.close()
    spy2 = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy2)
    b = e2.submit(waiver())
    assert b.status == "REPLAYED" and b.result == a.result and spy2.calls == []


def test_different_step_seq_is_a_different_action(ex, conn):
    base = n(conn, "offers")
    assert ex.submit(waiver(seq=0)).status == "OK"
    assert ex.submit(waiver(seq=1)).status == "OK"
    assert n(conn, "offers") == base + 2


def test_args_validated_after_normalisation_and_never_reach_the_mock(ex, spy):
    bad = ex.submit(ActionIntent("apply_fee_waiver", {"merchant_id": M, "months": 0}, case_id="C-1"))
    assert bad.status == "ERROR" and bad.error.startswith("INVALID_ARGS") and "months" in bad.error
    assert spy.calls == []
    # float amount from the model normalises to integer paise and validates
    ok = ex.submit(ActionIntent("create_refund_request", {"merchant_id": M, "payment_id": "P-0001", "amount_paise": 100.0}, case_id="C-2"))
    assert ok.error != "INVALID_ARGS" and not (ok.error or "").startswith("INVALID_ARGS")
    assert spy.writes()[0][1] == "/mock/refunds"


def test_float_vs_int_amount_same_key_and_single_action(ex, spy):
    a = ex.submit(ActionIntent("create_refund_request", {"merchant_id": M, "payment_id": "P-0001", "amount_paise": 100.0}, case_id="C-9"))
    b = ex.submit(ActionIntent("create_refund_request", {"merchant_id": M, "payment_id": "P-0001", "amount_paise": 100}, case_id="C-9"))
    assert a.idempotency_key == b.idempotency_key and b.status == "REPLAYED" and len(spy.writes()) == 1


def test_unknown_and_forbidden_actions_are_refused_and_never_reach_a_mock(ex, spy):
    for name in ("approve_kyc", "change_bank_details", "no_such_action"):
        out = ex.submit(ActionIntent(name, {"merchant_id": M}, case_id="C-1"))
        assert out.status == "REFUSED" and "registry" in out.error
    assert spy.calls == []
    assert [e.kind for e in ex.ledger.entries("C-1")] == ["POLICY"] * 3
    assert ex.ledger.entries("C-1")[0].payload["rule_ids"] == ["REGISTRY"]


# ------------------------------------------------------------------------------------------ policy gate
@pytest.mark.parametrize("policy,status,rule", [(DenyAll("TEST-DENY"), "REFUSED", "TEST-DENY"), (EscalateAll(), "ESCALATE", "TEST-ESC")])
def test_block_and_escalate_never_reach_the_mock(db_path, clock, http, conn, policy, status, rule):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy, policy=policy)
    tables = ["offers", "tickets", "comms_outbox", "schedules"]
    snap = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    for intent in (waiver(), ActionIntent("raise_ticket", {"merchant_id": M, "type": "DEVICE"}, case_id="C-1"),
                   ActionIntent("send_message", {"merchant_id": M, "channel": "app", "body": "hi"}, case_id="C-1")):
        out = e.submit(intent)
        assert out.status == status and [v.rule_id for v in out.violations] == [rule]
    assert spy.calls == []
    assert {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables} == snap
    assert n(e.conn, "actions") == 0 and n(e.conn, "idempotency") == 0 and n(e.conn, "outbox") == 0
    entries = e.ledger.entries("C-1")
    assert [x.kind for x in entries] == ["POLICY"] * 3 and all(x.payload["allow"] is False and x.payload["rule_ids"] == [rule] for x in entries)


def test_escalated_then_allowed_executes_normally(db_path, clock, http, conn):
    e = make_executor(db_path, clock, SpyHttp(http), policy=EscalateAll())
    assert e.submit(waiver()).status == "ESCALATE"
    e.policy = AllowAll()
    assert e.submit(waiver()).status == "OK"       # the refused attempt reserved nothing


# ------------------------------------------------------------------------------------------ crash safety
def test_crash_after_intent_before_mock_keeps_intent_and_restart_completes_once(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.crash_before = 1
    e1 = make_executor(db_path, clock, spy)
    base = n(conn, "offers")
    with pytest.raises(Crash):
        e1.submit(waiver())
    assert kinds(e1) == ["ACTION_INTENT", "POLICY"]                       # the intent survived the crash
    assert n(conn, "offers") == base                                       # nothing was executed
    assert e1.conn.execute("SELECT status FROM actions").fetchone()[0] == "PENDING"
    e1.conn.close()

    spy2 = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy2)
    out = e2.submit(waiver())                                              # the engine re-submits after restart
    assert out.status == "OK" and n(conn, "offers") == base + 1
    assert kinds(e2) == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]       # no second intent, no duplicate
    assert n(e2.conn, "actions") == 1
    assert e2.submit(waiver()).status == "REPLAYED" and n(conn, "offers") == base + 1


def test_crash_after_mock_commit_before_result_recorded_does_not_duplicate(db_path, clock, http, conn):
    spy = SpyHttp(http)
    spy.crash_after = 1
    e1 = make_executor(db_path, clock, spy)
    base = n(conn, "offers")
    with pytest.raises(Crash):
        e1.submit(waiver())
    assert n(conn, "offers") == base + 1 and kinds(e1) == ["ACTION_INTENT", "POLICY"]
    e1.conn.close()
    spy2 = SpyHttp(http)
    e2 = make_executor(db_path, clock, spy2)
    recovered = e2.recover()                                               # restart sweep, no engine involvement
    assert [o.status for o in recovered] == ["OK"]
    assert n(conn, "offers") == base + 1                                   # the mock replayed; still exactly one row
    assert spy2.writes()[0][2] == recovered[0].idempotency_key
    assert kinds(e2) == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]
    assert recovered[0].result["status"] == "APPLIED"
    assert e2.recover() == []


def test_unexpected_exception_from_the_transport_is_an_honest_error_and_intent_stays_ledgered(ex, spy, conn):
    spy.fail_with = RuntimeError("socket exploded")
    base = n(conn, "offers")
    out = ex.submit(waiver())
    assert out.status == "ERROR" and "UPSTREAM_UNAVAILABLE" in out.error and n(conn, "offers") == base
    assert kinds(ex) == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]
    assert ex.ledger.entries("C-1")[-1].payload["status"] == "ERROR"
    spy.fail_with = None
    again = ex.submit(waiver())                                            # same intent later resumes, same key
    assert again.status == "OK" and again.idempotency_key == out.idempotency_key and n(conn, "offers") == base + 1
    assert n(ex.conn, "actions") == 1 and kinds(ex).count("ACTION_INTENT") == 1


def test_ledger_failure_before_any_side_effect_leaves_no_action_and_no_mock_call(db_path, clock, http, conn, monkeypatch):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    real = e.ledger.append

    def boom(**kw):
        if kw["kind"] == "POLICY":
            raise OSError("disk full")
        return real(**kw)

    monkeypatch.setattr(e.ledger, "append", boom)
    with pytest.raises(OSError):
        e.submit(waiver())
    assert spy.calls == [] and n(e.conn, "actions") == 0 and n(e.conn, "idempotency") == 0 and e.ledger.entries() == []
    assert not e.conn.in_transaction


# ------------------------------------------------------------------------------------------ retries & chaos
def chaos(http, system, mode, rate):
    assert http.post("/mock/chaos", json={"system": system, "mode": mode, "rate": rate}).status_code == 200


def test_timeout_after_commit_retries_same_key_and_yields_one_row_one_result(db_path, clock, http, conn):
    spy = SpyHttp(http)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        chaos(http, "pricing", "timeout_after_commit", 0)                 # the network recovers while we back off

    e = make_executor(db_path, clock, spy, sleep=sleep)
    chaos(http, "pricing", "timeout_after_commit", 1.0)
    base = n(conn, "offers")
    out = e.submit(waiver())
    assert out.status == "OK" and out.result["status"] == "APPLIED"
    assert n(conn, "offers") == base + 1                                   # committed once at the mock, then replayed
    keys = {c[2] for c in spy.writes()}
    assert len(spy.writes()) == 2 and keys == {out.idempotency_key}       # SAME key on the retry
    assert sleeps == [1.0]
    assert kinds(e).count("ACTION_RESULT") == 1 and n(e.conn, "actions") == 1
    assert e.breaker.state("pricing") == "CLOSED"


def test_retry_with_a_fresh_key_per_attempt_double_applies__so_the_executor_must_not(db_path, clock, http, conn):
    """Mutation test: the uuid4-per-retry mistake really does create two waivers; the real executor creates one."""
    def run(fresh, merchant):
        spy = SpyHttp(http)
        spy.fresh_keys = fresh
        e = make_executor(db_path, clock, spy, sleep=lambda s: chaos(http, "pricing", "timeout_after_commit", 0))
        chaos(http, "pricing", "timeout_after_commit", 1.0)
        before = n(conn, "offers", "merchant_id = ?", (merchant,))
        assert e.submit(waiver(merchant=merchant)).status == "OK"
        chaos(http, "pricing", "timeout_after_commit", 0)
        return n(conn, "offers", "merchant_id = ?", (merchant,)) - before

    assert run(True, "M-0002") == 2          # buggy transport: duplicate
    assert run(False, "M-0003") == 1         # the executor: exactly once


def test_error500_retries_twice_with_exponential_backoff_then_errors_and_counts_for_the_breaker(db_path, clock, http, conn):
    spy = SpyHttp(http)
    sleeps = []
    e = make_executor(db_path, clock, spy, sleep=sleeps.append)
    chaos(http, "pricing", "error500", 1.0)
    base = n(conn, "offers")
    out = e.submit(waiver())
    assert out.status == "ERROR" and out.error.startswith("UPSTREAM_UNAVAILABLE") and "3 attempt" in out.error
    assert len(spy.writes()) == 3 and {c[2] for c in spy.writes()} == {out.idempotency_key}
    assert sleeps == [1.0, 2.0]
    assert n(conn, "offers") == base
    assert e.breaker.failures("pricing") == 1 and e.breaker.state("pricing") == "CLOSED"
    assert e.conn.execute("SELECT status FROM actions").fetchone()[0] == "FAILED"


def test_business_4xx_is_not_retried_and_is_replayed_with_its_error_code(ex, spy):
    out = ex.submit(waiver(merchant="M-9999"))
    assert out.status == "ERROR" and out.error == "MERCHANT_NOT_FOUND" and out.result["error"] == "MERCHANT_NOT_FOUND"
    assert len(spy.writes()) == 1
    assert ex.breaker.failures("pricing") == 0                            # a 4xx means the system is alive
    again = ex.submit(waiver(merchant="M-9999"))
    assert again.status == "REPLAYED" and again.error == "MERCHANT_NOT_FOUND" and len(spy.writes()) == 1
    assert ex.ledger.entries("C-1")[-1].payload["error"] == "MERCHANT_NOT_FOUND"


def test_dead_mock_gives_an_honest_answer_not_a_hang(db_path, clock, http, conn):
    spy = SpyHttp(http)
    e = make_executor(db_path, clock, spy)
    chaos(http, "pricing", "error500", 1.0)
    for i in range(3):
        assert e.submit(waiver(seq=i)).status == "ERROR"
    calls = len(spy.calls)
    out = e.submit(waiver(seq=3))
    assert out.status == "ERROR" and out.error.startswith("CIRCUIT_OPEN") and "nothing was sent" in out.error
    assert len(spy.calls) == calls            # breaker open: the mock was not touched


# ------------------------------------------------------------------------------------------ reads
def test_reads_are_ledgered_with_a_light_payload_and_never_replayed(ex, spy):
    r = ex.submit(ActionIntent("get_offer_band", {"merchant_id": M}, case_id="C-r"))
    r2 = ex.submit(ActionIntent("get_offer_band", {"merchant_id": M}, case_id="C-r"))
    assert r.status == r2.status == "OK" and r.result["max_months"] >= 1     # live read twice, not REPLAYED
    assert len(spy.calls) == 2 and all(c[0] == "GET" and c[2] is None for c in spy.calls)
    es = ex.ledger.entries("C-r")
    assert [e.kind for e in es] == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"] * 2
    assert es[2].payload == {"action": "get_offer_band", "key": r.idempotency_key, "status": "OK", "cls": "R0"}   # light: no body
    assert n(ex.conn, "actions") == 0


def test_reader_callable_reads_through_the_executor(ex):
    read = ex.reader("C-rd")
    body = read("get_offer_band", {"merchant_id": M})
    assert body["merchant_id"] == M and "max_months" in body
    assert [e.kind for e in ex.ledger.entries("C-rd")] == ["ACTION_INTENT", "ACTION_RESULT"]
    with pytest.raises(ReadError) as ei:
        read("get_offer_band", {"merchant_id": "M-9999"})
    assert ei.value.outcome.error == "MERCHANT_NOT_FOUND"
    with pytest.raises(ValueError):
        ex.read("apply_fee_waiver", {"merchant_id": M, "months": 1})


def test_policy_receives_a_working_reader(db_path, clock, http):
    got = {}

    class Reading:
        policy_hash = "p"

        def evaluate(self, intent, reader=None):
            got["band"] = reader("get_offer_band", {"merchant_id": intent.args["merchant_id"]})
            return PolicyDecision("ALLOW", (), "p")

    e = make_executor(db_path, clock, SpyHttp(http), policy=Reading())
    assert e.submit(waiver()).status == "OK" and got["band"]["merchant_id"] == M


# ------------------------------------------------------------------------------------------ principals, kill switch, drafts
def test_human_principal_uses_the_same_pipeline_and_ledger_identity(ex):
    human = Principal("HUMAN", "priya", "support_executive")
    out = ex.submit(waiver(principal=human))
    assert out.status == "OK"
    es = ex.ledger.entries("C-1")
    assert [e.kind for e in es] == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"]
    assert {(e.actor, e.role) for e in es} == {("HUMAN:priya", "support_executive")}


def test_kill_switch_refuses_writes_but_not_reads(ex, spy, conn):
    ex.conn.execute("INSERT INTO meta(key, value) VALUES ('kill_switch','on')")
    base = n(conn, "offers")
    out = ex.submit(waiver())
    assert out.status == "REFUSED" and out.error.startswith("KILL_SWITCH") and [v.rule_id for v in out.violations] == ["KILL_SWITCH"]
    assert spy.calls == [] and n(conn, "offers") == base and n(ex.conn, "actions") == 0
    assert ex.ledger.entries("C-1")[0].payload["rule_ids"] == ["KILL_SWITCH"]
    assert ex.submit(ActionIntent("get_offer_band", {"merchant_id": M}, case_id="C-1")).status == "OK"
    ex.conn.execute("UPDATE meta SET value='off' WHERE key='kill_switch'")
    assert ex.submit(waiver()).status == "OK"


def test_draft_has_no_side_effect_and_no_http(ex, spy):
    out = ex.submit(ActionIntent("draft_message", {"merchant_id": M, "text": "ನಮಸ್ಕಾರ"}, case_id="C-d"))
    assert out.status == "OK" and out.result["committed"] is False and out.result["draft"]["text"] == "ನಮಸ್ಕಾರ"
    assert spy.calls == []


# ------------------------------------------------------------------------------------------ "only the executor calls a mock"
def test_no_module_outside_the_executor_calls_a_mock_directly():
    allowed_dirs = (ROOT / "packs" / "paytm_merchant" / "mocks",)
    allowed_files = {ROOT / "core" / "actions" / "executor.py", ROOT / "packs" / "paytm_merchant" / "actions.py",   # the registry holds path templates
                     # The consumer pack (#89) plays the same two roles: its registry holds path templates, and mocks.py IS its mock app.
                     ROOT / "packs" / "paytm_consumer" / "actions.py", ROOT / "packs" / "paytm_consumer" / "mocks.py",
                     # The lender pack (#114) is built the same way as the consumer pack.
                     ROOT / "packs" / "demo_lender" / "actions.py", ROOT / "packs" / "demo_lender" / "mocks.py",
                     # Demo/test scenario controls (chaos, 'customer pays', the in-process dev stack). Not agent actions: none of it is in the
                     # action registry, so no model can reach it. Keep this list at exactly these two files.
                     ROOT / "core" / "api" / "routes_control.py", ROOT / "core" / "api" / "dev_server.py"}
    pattern = re.compile(r"MockClient|TestClient|create_app\b|paytm_merchant\.mocks|[\"'`]/mock/")
    offenders = []
    for top in ("core", "packs", "jobs", "integrations"):
        for p in (ROOT / top).rglob("*.py"):
            if p in allowed_files or any(d in p.parents for d in allowed_dirs):
                continue
            if pattern.search(p.read_text()):
                offenders.append(str(p.relative_to(ROOT)))
    assert offenders == [], f"direct mock access outside core/actions/executor.py: {offenders}"


def test_the_grep_test_can_actually_fail(tmp_path):
    pattern = re.compile(r"MockClient|TestClient|create_app\b|paytm_merchant\.mocks|[\"'`]/mock/")
    assert pattern.search("from fastapi.testclient import TestClient") and pattern.search("x = client.get('/mock/offers')") and pattern.search("create_app(db, clock)") and not pattern.search("create_approval(x)")


# ------------------------------------------------------------------------------------------ internal actions
def test_save_preference_roundtrip_is_internal(ex, spy):
    out = ex.submit(ActionIntent("save_preference", {"merchant_id": M, "key": "lang", "value": "kn"}, case_id="C-p"))
    assert out.status == "OK" and out.result["previous_value"] is None and spy.calls == []
    assert ex.conn.execute("SELECT value FROM meta WHERE key='pref:M-0001:lang'").fetchone()[0] == "kn"
