"""The Action Executor (#25): the ONLY module that talks to a mock system.

Order of effects for a write (the ledger is written BEFORE any side effect, never after):

    validate args -> policy.evaluate
      -> [one transaction] actions row + idempotency reservation (+ outbox row for R3)
                           + ledger ACTION_INTENT + ledger POLICY
      -> call the mock with the Idempotency-Key header (retries re-use the SAME key)
      -> [one transaction] record result + register compensation + mark outbox sent + ledger ACTION_RESULT

If the process dies between the two transactions the intent is already on the ledger and the action is
PENDING; re-submitting the same intent (or ``recover()`` / ``outbox.deliver_pending()``) finishes it with
the same key, so the mock replays instead of executing twice. R0 reads are ledgered too, with a lighter payload.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Callable
from urllib.parse import quote

from pydantic import ValidationError

from core.actions import compensation as comp_mod
from core.actions.circuit_breaker import CircuitBreaker
from core.actions.compensation import CompensationRegistry
from core.actions.idempotency import IdempotencyStore, make_key, normalise_args
from core.actions.outbox import Outbox
from core.actions.registry import ActionRegistry, ActionSpec, ForbiddenAction, RiskClass, UnknownAction
from core.actions.types import ActionIntent, ActionOutcome, PolicyEngine, Principal, Reader, Violation
from core.audit.ledger import Ledger
from core.engine.clock import SimClock
from core.memory.store import next_id, write_txn

MAX_RETRIES = 2            # => at most 3 attempts per call
BACKOFF_BASE_S = 1.0       # 1 s, then 2 s (injected sleep; never the wall clock)
SYSTEM_PRINCIPAL = Principal("SYSTEM", "munim-executor")


class ReadError(Exception):
    def __init__(self, outcome: ActionOutcome):
        super().__init__(outcome.error)
        self.outcome = outcome


class _Transient(Exception):
    def __init__(self, reason: str, attempts: int):
        super().__init__(reason)
        self.reason, self.attempts = reason, attempts


def _error_code(status: int, body) -> str:
    if isinstance(body, dict):
        if isinstance(body.get("error"), str):
            return body["error"]
        d = body.get("detail")
        if isinstance(d, dict) and isinstance(d.get("error"), str):
            return d["error"]
    return f"HTTP_{status}"


class Executor:
    ledgers_actions = True  # tells the case engine not to write a second ACTION_INTENT / ACTION_RESULT for the same action
    def __init__(self, conn, clock: SimClock, ledger: Ledger, registry: ActionRegistry, http, policy: PolicyEngine,
                 breaker: CircuitBreaker | None = None, outbox: Outbox | None = None, sleep: Callable[[float], None] = lambda s: None):
        self.conn, self.clock, self.ledger, self.registry, self.http, self.policy = conn, clock, ledger, registry, http, policy
        self.breaker = breaker or CircuitBreaker(conn, clock, ledger)
        self.outbox = outbox or Outbox(conn, clock)
        self.outbox.bind(self._finish)
        self.sleep = sleep
        self.idem = IdempotencyStore(conn, clock)
        self.compensations = CompensationRegistry(conn, clock, registry)

    # ------------------------------------------------------------------------------------------ helpers
    def _txn(self):
        return write_txn(self.conn)

    def _led(self, kind: str, principal: Principal, case_id, payload: dict, policy_hash: str | None = None) -> None:
        self.ledger.append(kind=kind, actor=principal.ledger_actor, role=principal.role, case_id=case_id, payload=payload, policy_hash=policy_hash)

    def kill_switch_on(self) -> bool:
        return self.outbox.kill_switch_on()

    @staticmethod
    def _request(spec: ActionSpec, args: dict) -> tuple[str, str, dict | None]:
        path = spec.path.format(**{k: quote(str(v), safe="") for k, v in args.items()})
        body = None if spec.body_fields is None else {k: args[k] for k in spec.body_fields if k in args}
        return spec.method, path, body

    def _call(self, spec: ActionSpec, args: dict, key: str | None, probe: bool) -> tuple[int, dict, int]:
        """One logical call: up to 1 + MAX_RETRIES attempts (1 if this is the breaker's half-open probe), SAME key each time."""
        method, path, body = self._request(spec, args)
        attempts = 1 if probe else MAX_RETRIES + 1
        last = "unknown"
        for i in range(attempts):
            try:
                kw = {"headers": {"Idempotency-Key": key}} if key else {}
                r = self.http.request(method, path, json=body, **kw)
            except Exception as e:  # noqa: BLE001 - transport failure: the call may or may not have happened
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 504:
                    last = "timeout"
                elif r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                else:
                    try:
                        data = r.json()
                    except Exception:  # noqa: BLE001
                        data = {"raw": r.text}
                    return r.status_code, data, i + 1
            if i < attempts - 1:
                self.sleep(BACKOFF_BASE_S * (2 ** i))
        raise _Transient(last, attempts)

    # --------------------------------------------------------------------------------------- public API
    def reader(self, case_id: str | None = None, principal: Principal = SYSTEM_PRINCIPAL) -> Reader:
        """A core.actions.types.Reader: live R0 reads that go through the executor (ledgered, breaker-guarded)."""
        def read(name: str, args: dict) -> dict:
            out = self.read(name, args, case_id=case_id, principal=principal)
            if out.status != "OK":
                raise ReadError(out)
            return out.result
        return read

    def read(self, action: str, args: dict, *, case_id: str | None = None, principal: Principal = SYSTEM_PRINCIPAL) -> ActionOutcome:
        """R0 read without policy (policy itself reads through this). Ledgered with a light INTENT + RESULT."""
        spec = self.registry.get(action)
        if spec.cls is not RiskClass.R0:
            raise ValueError(f"{action} is not an R0 read")
        return self._read(ActionIntent(action, args, case_id=case_id, principal=principal), decision=None)

    def submit(self, intent: ActionIntent) -> ActionOutcome:
        return self._submit(intent)

    def undo(self, action_id: str, principal: Principal) -> ActionOutcome:
        return comp_mod.undo(self, action_id, principal)

    def compensate_pending_r1(self, case_id: str):
        return comp_mod.compensate_pending_r1(self, case_id)

    def deliver_pending(self) -> list[ActionOutcome]:
        return self.outbox.deliver_pending()

    def recover(self) -> list[ActionOutcome]:
        """After a restart: finish actions whose intent was ledgered but whose result never was (non-outbox)."""
        if self.kill_switch_on():
            return []
        rows = self.conn.execute(
            "SELECT action_id FROM actions WHERE status IN ('PENDING','FAILED') AND action_id NOT IN (SELECT action_id FROM outbox) ORDER BY rowid").fetchall()
        return [self._finish(r["action_id"], SYSTEM_PRINCIPAL) for r in rows]

    # ------------------------------------------------------------------------------------------ pipeline
    def _submit(self, intent: ActionIntent, *, bypass_kill: bool = False) -> ActionOutcome:
        pr = intent.principal
        try:
            spec = self.registry.get(intent.action)
        except (UnknownAction, ForbiddenAction) as e:
            msg = f"{type(e).__name__}: {intent.action!r} is not in the action registry"
            self._led("POLICY", pr, intent.case_id, {"action": intent.action, "allow": False, "rule_ids": ["REGISTRY"], "reason": msg})
            return ActionOutcome("REFUSED", "", error=msg, violations=[Violation("REGISTRY", "Unknown or forbidden action", "BLOCK", msg, action=intent.action, case_id=intent.case_id)])

        raw = normalise_args(intent.args)
        try:
            args = normalise_args(spec.args_model(**raw).model_dump(exclude_none=True))
        except ValidationError as e:
            key = make_key(intent.case_id, intent.action, raw, intent.step_seq)
            msg = "INVALID_ARGS: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            self._led("ACTION_RESULT", pr, intent.case_id, {"action": intent.action, "key": key, "status": "ERROR", "error": msg})
            return ActionOutcome("ERROR", key, error=msg)
        key = make_key(intent.case_id, intent.action, args, intent.step_seq)
        intent = dataclasses.replace(intent, args=args)

        if spec.cls is RiskClass.R0:
            decision = self.policy.evaluate(intent, self.reader(intent.case_id))
            if not decision.allowed:
                return self._not_allowed(intent, key, decision)
            return self._read(intent, decision=decision)

        # a finished action is replayed from the idempotency table; a half-done one (crash) is resumed with the same key
        existing = self.idem.get(key)
        if existing is not None:
            return self._replay_or_resume(existing, pr)

        if not bypass_kill and self.kill_switch_on():
            msg = "KILL_SWITCH: all writes are halted by the operator"
            self._led("POLICY", pr, intent.case_id, {"action": intent.action, "key": key, "allow": False, "rule_ids": ["KILL_SWITCH"], "reason": msg})
            return ActionOutcome("REFUSED", key, error=msg, violations=[Violation("KILL_SWITCH", "Kill switch on", "BLOCK", msg, action=intent.action, case_id=intent.case_id)])

        decision = self.policy.evaluate(intent, self.reader(intent.case_id))
        if not decision.allowed:
            return self._not_allowed(intent, key, decision)

        with self._txn():
            existing = self.idem.get(key)           # lost a race: someone reserved it between our check and the lock
            if existing is not None:
                action_id = None
            else:
                action_id = next_id(self.conn, "ACT")
                self.conn.execute(
                    "INSERT INTO actions(action_id, case_id, name, cls, args_json, idempotency_key, status, step_seq, ts_sim) VALUES (?,?,?,?,?,?, 'PENDING', ?, ?)",
                    (action_id, intent.case_id, spec.name, spec.cls.value, json.dumps(args, sort_keys=True), key, intent.step_seq, self.clock.now_iso()))
                self.idem.reserve(key, action_id)
                if spec.cls is RiskClass.R3:
                    m, p, b = self._request(spec, args) if spec.system else (None, None, None)
                    self.outbox.write(action_id=action_id, key=key, system=spec.system, method=m, path=p, body=b, principal=pr)
                self._led("ACTION_INTENT", pr, intent.case_id, {
                    "action": spec.name, "key": key, "cls": spec.cls.value, "action_id": action_id, "args": args,
                    "step_seq": intent.step_seq, "playbook_id": intent.playbook_id, "step": intent.step, "lane": intent.lane})
                self._led("POLICY", pr, intent.case_id, {
                    "action": spec.name, "key": key, "allow": True, "rule_ids": [v.rule_id for v in decision.violations],
                    "outcome": decision.outcome}, decision.policy_hash or None)
        if action_id is None:
            return self._replay_or_resume(existing, pr)
        return self._finish(action_id, pr)

    def _not_allowed(self, intent: ActionIntent, key: str, decision) -> ActionOutcome:
        rule_ids = [v.rule_id for v in decision.violations]
        self._led("POLICY", intent.principal, intent.case_id, {
            "action": intent.action, "key": key, "allow": False, "rule_ids": rule_ids, "outcome": decision.outcome,
            "reasons": [v.reason for v in decision.violations]}, decision.policy_hash or None)
        status = "ESCALATE" if decision.outcome == "ESCALATE" else "REFUSED"
        reason = "; ".join(f"{v.rule_id}: {v.reason}" for v in decision.violations) or decision.outcome
        return ActionOutcome(status, key, violations=list(decision.violations), error=reason)

    def _replay_or_resume(self, idem_row, principal: Principal) -> ActionOutcome:
        a = self.conn.execute("SELECT * FROM actions WHERE action_id = ?", (idem_row["action_id"],)).fetchone()
        if a["status"] in ("OK", "ERROR"):
            stored = json.loads(a["result_json"]) if a["result_json"] else None
            if a["status"] == "OK":
                return ActionOutcome("REPLAYED", a["idempotency_key"], action_id=a["action_id"], result=stored)
            return ActionOutcome("REPLAYED", a["idempotency_key"], action_id=a["action_id"], result=stored.get("body"), error=stored.get("error"))
        if self.kill_switch_on() and self.outbox.get(a["action_id"]) is not None:
            return ActionOutcome("REFUSED", a["idempotency_key"], action_id=a["action_id"], error="KILL_SWITCH: queued delivery is held")
        return self._finish(a["action_id"], principal)

    # ------------------------------------------------------------------------------------------ execution
    def _finish(self, action_id: str, principal: Principal) -> ActionOutcome:
        a = self.conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if a["status"] in ("OK", "ERROR"):               # delivered by someone else meanwhile
            return self._replay_or_resume(self.idem.get(a["idempotency_key"]), principal)
        spec = self.registry.get(a["name"])
        args, key, case_id = json.loads(a["args_json"]), a["idempotency_key"], a["case_id"]
        if self.outbox.get(action_id):
            with self._txn():
                self.outbox.bump(action_id)
        if spec.system is None:
            with self._txn():
                return self._record_ok(a, spec, args, self._internal(spec, args), principal, 0)
        ok, probe = self.breaker.allow(spec.system)
        if not ok:
            wait = self.breaker.retry_after_s(spec.system)
            return self._record_transient(a, principal, f"CIRCUIT_OPEN: {spec.system} is unavailable (circuit open, next probe in {wait:g}s); nothing was sent", 0)
        try:
            status, body, attempts = self._call(spec, args, key, probe)
        except _Transient as t:
            self.breaker.record_failure(spec.system)
            return self._record_transient(a, principal, f"UPSTREAM_UNAVAILABLE: {spec.system} did not answer ({t.reason}) after {t.attempts} attempt(s)", t.attempts)
        self.breaker.record_success(spec.system)
        if 200 <= status < 300:
            with self._txn():
                return self._record_ok(a, spec, args, body, principal, attempts)
        return self._record_business_error(a, principal, status, body, attempts)

    def _internal(self, spec: ActionSpec, args: dict) -> dict:
        """Actions with no mock system. Runs inside the same transaction that records the result."""
        if spec.cls is RiskClass.DRAFT:
            return {"draft": args, "committed": False}
        if spec.name in ("save_preference", "restore_preference"):
            k = f"pref:{args['merchant_id']}:{args['key']}"
            prev = self.conn.execute("SELECT value FROM meta WHERE key = ?", (k,)).fetchone()
            if args["value"] == "":
                self.conn.execute("DELETE FROM meta WHERE key = ?", (k,))
            else:
                self.conn.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, args["value"]))
            return {"merchant_id": args["merchant_id"], "key": args["key"], "value": args["value"], "previous_value": prev["value"] if prev else None}
        raise ValueError(f"no internal handler for {spec.name}")

    def _record_ok(self, a, spec: ActionSpec, args: dict, body: dict, principal: Principal, attempts: int) -> ActionOutcome:
        action_id, key, case_id = a["action_id"], a["idempotency_key"], a["case_id"]
        rj = json.dumps(body, sort_keys=True)
        self.conn.execute("UPDATE actions SET status='OK', result_json=? WHERE action_id=?", (rj, action_id))
        self.idem.complete(key, rj)
        until = self.compensations.register(action_id, spec, args, body)
        if self.outbox.get(action_id):
            self.outbox.mark_sent(action_id)
        self._led("ACTION_RESULT", principal, case_id, {"action": spec.name, "key": key, "status": "OK", "action_id": action_id,
                                                         "attempts": attempts, "result": body})
        return ActionOutcome("OK", key, action_id=action_id, result=body, compensable_until=until)

    def _record_business_error(self, a, principal: Principal, status: int, body, attempts: int) -> ActionOutcome:
        action_id, key = a["action_id"], a["idempotency_key"]
        code = _error_code(status, body)
        stored = json.dumps({"error": code, "http_status": status, "body": body}, sort_keys=True)
        with self._txn():
            self.conn.execute("UPDATE actions SET status='ERROR', result_json=? WHERE action_id=?", (stored, action_id))
            self.idem.complete(key, stored)
            if self.outbox.get(action_id):
                self.outbox.mark_sent(action_id, "FAILED")
            self._led("ACTION_RESULT", principal, a["case_id"], {"action": a["name"], "key": key, "status": "ERROR", "action_id": action_id,
                                                                  "error": code, "http_status": status, "attempts": attempts})
        return ActionOutcome("ERROR", key, action_id=action_id, result=body, error=code)

    def _record_transient(self, a, principal: Principal, reason: str, attempts: int) -> ActionOutcome:
        action_id, key = a["action_id"], a["idempotency_key"]
        with self._txn():
            self.conn.execute("UPDATE actions SET status='FAILED' WHERE action_id=?", (action_id,))
            self._led("ACTION_RESULT", principal, a["case_id"], {"action": a["name"], "key": key, "status": "ERROR", "action_id": action_id,
                                                                  "error": reason, "retryable": True, "attempts": attempts})
        return ActionOutcome("ERROR", key, action_id=action_id, error=reason)

    # ------------------------------------------------------------------------------------------ reads
    def _read(self, intent: ActionIntent, decision) -> ActionOutcome:
        spec = self.registry.get(intent.action)
        pr = intent.principal
        raw = normalise_args(intent.args)
        try:
            args = normalise_args(spec.args_model(**raw).model_dump(exclude_none=True))
        except ValidationError as e:
            return ActionOutcome("ERROR", "", error="INVALID_ARGS: " + str(e))
        key = make_key(intent.case_id, spec.name, args, intent.step_seq)
        with self._txn():
            self._led("ACTION_INTENT", pr, intent.case_id, {"action": spec.name, "key": key, "cls": "R0", "args": args})
            if decision is not None:
                self._led("POLICY", pr, intent.case_id, {"action": spec.name, "key": key, "allow": True,
                                                         "rule_ids": [v.rule_id for v in decision.violations], "outcome": decision.outcome}, decision.policy_hash or None)

        def done(status: str, **extra):
            self._led("ACTION_RESULT", pr, intent.case_id, {"action": spec.name, "key": key, "status": status, "cls": "R0", **extra})

        ok, probe = self.breaker.allow(spec.system)
        if not ok:
            msg = f"CIRCUIT_OPEN: {spec.system} is unavailable (circuit open); nothing was read"
            done("ERROR", error=msg)
            return ActionOutcome("ERROR", key, error=msg)
        try:
            status, body, attempts = self._call(spec, args, None, probe)
        except _Transient as t:
            self.breaker.record_failure(spec.system)
            msg = f"UPSTREAM_UNAVAILABLE: {spec.system} did not answer ({t.reason}) after {t.attempts} attempt(s)"
            done("ERROR", error=msg)
            return ActionOutcome("ERROR", key, error=msg)
        self.breaker.record_success(spec.system)
        if 200 <= status < 300:
            done("OK")
            return ActionOutcome("OK", key, result=body)
        code = _error_code(status, body)
        done("ERROR", error=code, http_status=status)
        return ActionOutcome("ERROR", key, result=body, error=code)
