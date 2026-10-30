"""The engine loop (Bible section 15).

Crash safety: for every event the engine persists (1) the EVENT ledger entry together with an
``engine_progress`` row, and (2) every planned ActionIntent together with its ACTION_INTENT ledger entry
in ``engine_intents``, BEFORE the executor is called. ``recover()`` finishes whatever is unfinished;
re-submitting an intent reuses its step_seq, so the executor's idempotency key makes the effect happen once.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from core.actions.executor import ReadError
from core.actions.registry import ActionRegistry
from core.actions.types import ActionIntent, ActionOutcome, Executor, Reader
from core.audit.ledger import Ledger
from core.engine.case_manager import FIX_FIRST_RULE, CaseManager
from core.engine.cases import LIVE, TERMINAL, Case, CaseRepo
from core.engine.clock import SimClock
from core.engine.events import append_event
from core.memory.store import write_txn

MESSAGE_ACTION = "send_message"
MERCHANT_INITIATED = frozenset({"MERCHANT_MESSAGE", "LEAD_REPLY"})
REPLY_WINDOW_MINUTES = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_progress (
  event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN'
);
CREATE TABLE IF NOT EXISTS engine_intents (
  case_id TEXT NOT NULL, step_seq INTEGER NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
  step TEXT, intent_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', outcome_json TEXT,
  PRIMARY KEY (case_id, step_seq)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    merchant_id: str | None = None
    case_id: str | None = None
    payload: dict = field(default_factory=dict)

    @staticmethod
    def coerce(obj: Any) -> "Event":
        if isinstance(obj, Event):
            return obj
        return Event(obj["event_id"], obj["type"], obj.get("merchant_id"), obj.get("case_id"), dict(obj.get("payload") or {}))


@dataclass
class Decision:
    """What the turn agent (Phase 3) hands back. ``action`` is (name, args)."""
    action: tuple[str, dict] | None = None
    message_en: str | None = None
    follow_up_hours: float | None = None
    channel: str | None = None
    language: str | None = None                       # the reply language (the turn agent's); default: the case's language
    escalate: list[str] = field(default_factory=list)  # reasons: hand the case to a human AFTER any message in this decision is sent
    park: str | None = None                           # a resume note: park the case (fix-first) instead of acting
    answers_merchant: bool = False                    # set by CODE only: this message completes the merchant's own recent request (a promised
                                                      # re-check), so it is a reply, not outreach; honoured only within REPLY_WINDOW of their message


@dataclass
class DecisionContext:
    case: Case
    step: Any
    playbook: Any
    event: Event
    allowlist: tuple[str, ...]
    facts: dict
    open_cases: list[Case]


@dataclass
class EventResult:
    status: str            # PROCESSED | DUPLICATE | IGNORED | NO_CASE
    case_id: str | None = None
    state: str | None = None


def _intent_to_json(i: ActionIntent) -> str:
    return json.dumps({"action": i.action, "args": i.args, "case_id": i.case_id, "step_seq": i.step_seq, "playbook_id": i.playbook_id,
                       "step": i.step, "proactive": i.proactive, "lane": i.lane, "open_case_lanes": list(i.open_case_lanes),
                       "channel": i.channel, "allowlist": None if i.allowlist is None else list(i.allowlist), "facts": i.facts},
                      sort_keys=True)


def _intent_from_json(s: str) -> ActionIntent:
    d = json.loads(s)
    d["open_case_lanes"] = tuple(d["open_case_lanes"])
    d["allowlist"] = None if d["allowlist"] is None else tuple(d["allowlist"])
    return ActionIntent(**d)


class Engine:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger: Ledger, registry: ActionRegistry,
                 playbooks: Mapping[str, Any], reader: Reader, executor: Executor, diagnose_fn: Callable, decider: Callable,
                 hard_escalation: Callable[[Case, Event], list[str]] | None = None, timers=None, *,
                 case_manager: CaseManager | None = None, done_fn: Callable | None = None, facts_fn: Callable | None = None,
                 contact_budget=None, kill_switch: Callable[[], bool] | None = None, max_iterations: int = 6,
                 on_escalate: Callable[[Case, list[str], list], Any] | None = None, hold_fn: Callable[[Case], str | None] | None = None,
                 resolution_fn: Callable[[Case, "Event", list[Case]], "Decision | None"] | None = None,
                 refused_message_line: Callable[[str | None], str | None] | None = None):
        self.conn, self.clock, self.ledger, self.registry = conn, clock, ledger, registry
        # language -> the pack's fixed hand-over line. When the policy engine refuses a reply, the merchant gets this line (itself policy-checked)
        # and a person takes the case, instead of silence. Without it a refused reply blocks the case, as before.
        self.refused_message_line = refused_message_line
        self.playbooks, self.reader, self.executor = playbooks, reader, executor
        self.diagnose_fn, self.decider = diagnose_fn, decider
        self.hard_escalation = hard_escalation or (lambda case, event: [])
        self.timers = timers
        self.repo = CaseRepo(conn, clock, ledger)
        self.case_manager = case_manager or CaseManager(self.repo, registry)
        self.done_fn, self.facts_fn = done_fn, facts_fn
        self.contact_budget, self.kill_switch = contact_budget, kill_switch
        self.max_iterations = max_iterations
        self._follow_up: float | None = None
        self._executor_ledgers = bool(getattr(executor, "ledgers_actions", False))
        self.on_escalate = on_escalate   # e.g. HandoffService.create_packet; when set it is the ONLY writer of the ESCALATION entry
        self.hold_fn = hold_fn           # e.g. IncidentManager.hold_note: a case held from outside (an area incident) parks instead of acting
        self.resolution_fn = resolution_fn  # e.g. TurnAgent.resolution_notice: what a merchant is told when their own message closes a case unprompted
        ensure_schema(conn)
        if timers is not None:
            timers.set_handler(self.handle_event)

    # ------------------------------------------------------------------ cases
    def open_case(self, merchant_id: str | None, playbook_id: str, *, lead_id: str | None = None, language: str | None = None) -> Case:
        pb = self.playbooks[playbook_id]
        return self.repo.open_case(playbook_id=playbook_id, lane=pb.priority_lane, merchant_id=merchant_id, lead_id=lead_id, language=language)

    def escalate(self, case: Case, reasons: list[str], violations: list | tuple = ()) -> Case:
        """A human owns the case from here. With an ``on_escalate`` service attached (the handoff service) it writes the one
        ESCALATION ledger entry, carrying the handoff id, owner and deadline; without one the engine writes a minimal entry itself.
        Never both: two entries per escalation would double-count in replay.
        If the case was handling the merchant's own message and nothing has gone to that merchant since, they get the fixed hand-over line first."""
        self._handover_if_silent(case)
        with write_txn(self.conn):
            if self.on_escalate is None:
                self.ledger.append(kind="ESCALATION", actor="SYSTEM", case_id=case.case_id, payload={"reasons": list(reasons)})
            case = self.repo.transition(case, "ESCALATED", reason="escalated")
        if self.on_escalate is not None:
            self.on_escalate(case, list(reasons), list(violations))
        if self.timers:
            self.timers.cancel_for_case(case.case_id, reason="escalated")
        return case

    def close(self, case: Case, reason: str = "done_check_passed") -> Case:
        case = self.repo.transition(case, "RESOLVED", reason=reason)
        if self.timers:
            self.timers.cancel_for_case(case.case_id, reason="case_closed")
        if case.subject_id:
            self.case_manager.release_parked(case.subject_id)
        return case

    # ------------------------------------------------------------------ events
    def handle_event(self, event: Any) -> EventResult:
        ev = Event.coerce(event)
        case_id = ev.case_id or ev.payload.get("case_id")
        if not case_id:
            return EventResult("NO_CASE")
        case = self.repo.load(case_id)
        with write_txn(self.conn):
            cur = self.conn.execute("INSERT OR IGNORE INTO engine_progress(event_id, case_id, status) VALUES (?,?, 'OPEN')", (ev.event_id, case_id))
            if cur.rowcount == 0:
                return EventResult("DUPLICATE", case_id, case.state)
            append_event(self.conn, event_id=ev.event_id, type=ev.type, payload=ev.payload, ts_sim=self.clock.now_iso(),
                         merchant_id=case.merchant_id, case_id=case_id)
            self.repo.link_event(case_id, ev.event_id)
            self.ledger.append(kind="EVENT", actor="SYSTEM", case_id=case_id, payload={"type": ev.type, "event_id": ev.event_id})
        return self._drive(ev.event_id)

    def recover(self) -> list[str]:
        """After a restart: finish every event whose processing did not complete."""
        done = []
        for r in self.conn.execute("SELECT event_id FROM engine_progress WHERE status = 'OPEN' ORDER BY rowid").fetchall():
            self._drive(r["event_id"])
            done.append(r["event_id"])
        return done

    def _load_event(self, event_id: str) -> Event:
        # The case an event drives is recorded in engine_progress. The events row may predate the case (a mock's webhook
        # inserted it with no case_id and the engine's own INSERT OR IGNORE then changes nothing), so never trust events.case_id.
        r = self.conn.execute("SELECT e.*, p.case_id AS driven_case FROM events e JOIN engine_progress p ON p.event_id = e.event_id "
                              "WHERE e.event_id = ?", (event_id,)).fetchone()
        return Event(r["event_id"], r["type"], r["merchant_id"], r["driven_case"], json.loads(r["payload_json"]))

    def _drive(self, event_id: str) -> EventResult:
        ev = self._load_event(event_id)
        case = self.repo.load(ev.case_id)
        self._turn = (ev, self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0])   # for _handover_if_silent
        status = "PROCESSED"
        if case.state in TERMINAL or (self.kill_switch and self.kill_switch()):
            status = "IGNORED"
        else:
            self._run(case, ev)
        with write_txn(self.conn):
            self.conn.execute("UPDATE engine_progress SET status = 'DONE' WHERE event_id = ?", (event_id,))
        return EventResult(status, ev.case_id, self.repo.load(ev.case_id).state)

    # ------------------------------------------------------------------ the loop
    def _wake(self, case: Case, ev: Event) -> Case:
        if case.state == "ESCALATED" and ev.type == "HUMAN_RETURNED":
            case = self.repo.transition(case, "RETURNED_TO_AGENT", reason="human_returned", event_id=ev.event_id)
        if case.state == "NOT_NOW" and ev.type != "TIMER_FIRED":
            return case
        if case.state in ("NEW", "WAITING", "RETURNED_TO_AGENT", "BLOCKED", "NOT_NOW"):
            case = self.repo.transition(case, "ACTIVE", reason=f"event {ev.type}", event_id=ev.event_id)
        return case

    def _facts(self, case: Case, ev: Event) -> dict:
        facts = {"case_id": case.case_id, "playbook_id": case.playbook_id, "step": case.step, "touches": case.touches,
                 "event_type": ev.type, "event": ev.payload}
        if self.facts_fn:
            facts.update(self.facts_fn(case, ev))
        return facts

    def _acted_steps(self, event_id: str) -> set[str]:
        return {r["step"] for r in self.conn.execute("SELECT step FROM engine_intents WHERE event_id = ?", (event_id,))}

    def _run(self, case: Case, ev: Event) -> None:
        self._follow_up = None
        case = self._wake(self.repo.load(case.case_id), ev)
        if case.state != "ACTIVE":
            if case.state == "ESCALATED" and ev.type in MERCHANT_INITIATED:
                self._handover_if_silent(case)          # a person has this case; a merchant who writes again is told so, never left in silence (no new escalation)
            if case.state in ("PARKED", "NOT_NOW") and ev.type in MERCHANT_INITIATED:
                # Held is not deaf: what a held merchant says (theft, a person, a regulator) still reaches a human.
                reasons = list(self.hard_escalation(case, ev) or [])
                if reasons:
                    self.escalate(case, reasons)
            return  # parked / not-now / escalated: the event is logged, nothing else happens
        note = self.hold_fn(case) if self.hold_fn else None
        if note:
            self.case_manager.park(case, note)
            return
        if self._execute_pending(case, ev) != "OK":
            return
        pb = self.playbooks[case.playbook_id]
        for _ in range(self.max_iterations):
            case = self.repo.load(case.case_id)
            if case.state != "ACTIVE":
                return
            try:
                facts = self._facts(case, ev)
                step = self.diagnose_fn(pb, self.reader, case.merchant_id or case.lead_id, facts)
            except ReadError as e:                        # a system that cannot be read is never guessed around: a human takes it
                self.escalate(case, [f"TOOL_FAILURE: {e.outcome.error or e.outcome.status}"])
                return
            if step is None:
                if ev.type in MERCHANT_INITIATED:          # what the merchant just said (theft, a person, a regulator) outranks "done"
                    reasons = list(self.hard_escalation(case, ev) or [])
                    if reasons:
                        self.escalate(case, reasons)
                        return
                if self.done_fn and self.done_fn(pb, self.reader, case.merchant_id or case.lead_id, facts):
                    self._resolve(case, ev, pb, facts)
                else:
                    self._wait(case, None)
                return
            if step.name != case.step:
                case = self.repo.transition(case, "ACTIVE", step=step.name, reason="step")
            if step.name in self._acted_steps(ev.event_id):
                self._wait(case, None)  # already acted on this step for this event: wait for the world to change
                return
            reasons = list(self.hard_escalation(case, ev) or [])
            if reasons:
                self.escalate(case, reasons)
                return
            open_cases = self.repo.open_cases(case.subject_id)
            allow = tuple(self.case_manager.allowed_actions(case, pb, open_cases))
            d = self.decider(DecisionContext(case, step, pb, ev, allow, facts, open_cases))
            case = self.repo.load(case.case_id)
            if case.state != "ACTIVE":
                return                                    # the decider escalated or parked through a service of its own
            if d is not None and d.park and not d.message_en and d.action is None:
                self.case_manager.park(case, d.park)
                return
            if d is None or (d.action is None and not d.message_en):
                if d is not None and d.escalate:
                    self.escalate(case, list(d.escalate))
                    return
                self._wait(case, d)
                return
            planned = self._plan(case, ev, pb, step, d, open_cases, allow, facts)
            if planned == "STOP":
                return
            if self._execute_pending(case, ev) != "OK":
                return
            if self._follow_up and self.timers:          # a promised re-check exists BEFORE the done-check runs again
                from datetime import timedelta
                self.timers.cancel_for_case(case.case_id, kind="follow_up", reason="rescheduled")
                self.timers.schedule(case.case_id, self.clock.now() + timedelta(hours=self._follow_up), "follow_up", {})
                self._follow_up = None
            if d.escalate:                                # the hand-off line has gone out; now a human owns the case
                self.escalate(self.repo.load(case.case_id), list(d.escalate))
                return
            if d.park:
                self.case_manager.park(self.repo.load(case.case_id), d.park)
                return
        self._wait(self.repo.load(case.case_id), None)

    def _nothing_said(self, case: Case) -> bool:
        return self.conn.execute("SELECT 1 FROM ledger WHERE case_id = ? AND kind = 'MESSAGE_SENT' LIMIT 1", (case.case_id,)).fetchone() is None

    def _resolve(self, case: Case, ev: Event, pb: Any, facts: dict) -> None:
        """Close a case whose done-check passed. A merchant who wrote in and has heard NOTHING in this case is never closed on in silence:
        the resolution hook (verified status text from system reads, through the gate) is planned and sent first, through the normal
        allowlist, policy and executor path."""
        if self.resolution_fn is not None and ev.type in MERCHANT_INITIATED and self._nothing_said(case):
            d = self.resolution_fn(case, ev, self.repo.open_cases(case.subject_id))
            case = self.repo.load(case.case_id)
            if case.state != "ACTIVE":
                return
            if d is not None and d.message_en:
                step = _Resolved(case.step or "resolved")
                open_cases = self.repo.open_cases(case.subject_id)
                allow = tuple(self.case_manager.allowed_actions(case, pb, open_cases))
                if self._plan(case, ev, pb, step, d, open_cases, allow, facts) == "STOP" or self._execute_pending(case, ev) != "OK":
                    return
                if d.escalate:
                    self.escalate(self.repo.load(case.case_id), list(d.escalate))
                    return
        self.close(self.repo.load(case.case_id))

    def _playbook_allows(self, case: Case, pb: Any, action: str, facts: dict) -> bool:
        """A playbook's max_per_case and precondition on an allowed action are enforced here, not just parsed."""
        if not hasattr(pb, "action"):
            return True
        from core.engine.diagnose import action_permitted
        prior = self.conn.execute("SELECT COUNT(*) FROM engine_intents WHERE case_id = ? AND json_extract(intent_json, '$.action') = ? "
                                  "AND status IN ('DONE', 'PENDING')", (case.case_id, action)).fetchone()[0]
        ok, why = action_permitted(pb, action, self.reader, case.merchant_id or case.lead_id, facts, prior)
        if not ok:
            self.ledger.append(kind="POLICY", actor="SYSTEM", case_id=case.case_id,
                               payload={"action": action, "allow": False, "rule_ids": ["PLAYBOOK-LIMIT"], "reason": why, "step": case.step})
        return ok

    def _is_proactive(self, action: str, ev: Event) -> bool:
        return self.registry.get(action).contact and ev.type not in MERCHANT_INITIATED

    def _recently_asked(self, case: Case) -> bool:
        """Did the merchant write to this case within the reply window? Checked from the event log, never taken on the decider's word."""
        from datetime import timedelta
        from core.engine.clock import iso
        since = iso(self.clock.now() - timedelta(minutes=REPLY_WINDOW_MINUTES))
        return self.conn.execute("SELECT 1 FROM case_events ce JOIN events e ON e.event_id = ce.event_id WHERE ce.case_id = ? AND e.type IN "
                                 "('MERCHANT_MESSAGE', 'LEAD_REPLY') AND e.ts_sim >= ? LIMIT 1", (case.case_id, since)).fetchone() is not None

    def _plan(self, case: Case, ev: Event, pb: Any, step: Any, d: Decision, open_cases: list[Case], allow: tuple[str, ...], facts: dict) -> str:
        lanes = tuple(c.lane for c in open_cases)
        subject = case.merchant_id or case.lead_id
        channel = d.channel or ev.payload.get("channel") or "whatsapp"
        planned: list[tuple[str, str, dict, bool]] = []  # kind, action, args, proactive
        if d.action:
            name, args = d.action
            if not self.registry.has(name):
                self._reject(case, step, name, "UNKNOWN-ACTION", f"{name} is not a registered action", park=False)
                return "STOP"
            proactive = self._is_proactive(name, ev)
            v = self.case_manager.intent_allowed(name, allow, lanes, case_lane=case.lane, proactive=proactive)
            if not v.ok:
                self._reject(case, step, name, v.rule_id, v.reason, park=(v.rule_id == FIX_FIRST_RULE))
                return "STOP"
            if not self._playbook_allows(case, pb, name, facts):
                self._wait(case, d)
                return "STOP"
            planned.append(("action", name, args, proactive))
        if d.message_en:
            proactive = self._is_proactive(MESSAGE_ACTION, ev) and not (d.answers_merchant and self._recently_asked(case))
            v = self.case_manager.intent_allowed(MESSAGE_ACTION, allow + (MESSAGE_ACTION,), lanes, case_lane=case.lane, proactive=proactive)
            if not v.ok:
                self._reject(case, step, MESSAGE_ACTION, v.rule_id, v.reason, park=(v.rule_id == FIX_FIRST_RULE))
                return "STOP"
            if not self._playbook_allows(case, pb, MESSAGE_ACTION, facts):
                self._wait(case, d)
                return "STOP"
            planned.append(("message", MESSAGE_ACTION,
                            {"merchant_id": subject, "channel": channel, "body": d.message_en, "language": d.language or case.language or "en",
                             "case_id": case.case_id}, proactive))
        # contact budget: a denied proactive contact is dropped (and ledgered), the rest goes on
        kept = []
        for kind, name, args, proactive in planned:
            if proactive and self.contact_budget is not None and self.registry.get(name).contact:
                ok, why = self.contact_budget.check(subject, channel, proactive=True, case_id=case.case_id)
                if not ok:
                    self.ledger.append(kind="POLICY", actor="SYSTEM", case_id=case.case_id,
                                       payload={"action": name, "allow": False, "rule_ids": ["CONTACT-BUDGET"], "reason": why})
                    continue
            kept.append((kind, name, args, proactive))
        if not kept:
            self._wait(case, d)
            return "STOP"
        with write_txn(self.conn):
            seq = self.conn.execute("SELECT COALESCE(MAX(step_seq), 0) FROM engine_intents WHERE case_id = ?", (case.case_id,)).fetchone()[0]
            self.ledger.append(kind="DECISION", actor="AGENT", case_id=case.case_id,
                               payload={"next_action": d.action[0] if d.action else None, "step": step.name, "message": bool(d.message_en)})
            for kind, name, args, proactive in kept:
                seq += 1
                intent = ActionIntent(action=name, args=args, case_id=case.case_id, step_seq=seq, playbook_id=case.playbook_id, step=step.name,
                                      proactive=proactive, lane=case.lane, open_case_lanes=lanes, channel=channel if self.registry.get(name).contact else None,
                                      allowlist=allow + ((MESSAGE_ACTION,) if MESSAGE_ACTION not in allow else ()), facts=_json_safe(facts))
                if not self._executor_ledgers:  # the real executor ledgers INTENT/POLICY/RESULT itself; never record an action twice
                    self.ledger.append(kind="ACTION_INTENT", actor="AGENT", case_id=case.case_id,
                                       payload={"action": name, "step_seq": seq, "step": step.name})
                self.conn.execute("INSERT INTO engine_intents(case_id, step_seq, event_id, kind, step, intent_json) VALUES (?,?,?,?,?,?)",
                                  (case.case_id, seq, ev.event_id, kind, step.name, _intent_to_json(intent)))
        self._follow_up = d.follow_up_hours
        return "OK"

    def _reject(self, case: Case, step: Any, action: str, rule_id: str | None, reason: str | None, *, park: bool) -> None:
        self.ledger.append(kind="POLICY", actor="SYSTEM", case_id=case.case_id,
                           payload={"action": action, "allow": False, "rule_ids": [rule_id], "reason": reason, "step": step.name})
        case = self.repo.load(case.case_id)
        if park:
            self.case_manager.park(case, f"I'll come back to {case.playbook_id} once your other issue is sorted.")
        elif self._merchant_waiting():                       # the merchant asked and nothing will answer them: a person takes it (found live, C2)
            self.escalate(case, [f"{rule_id}: {reason}"])
        else:
            self.repo.transition(case, "BLOCKED", step=step.name, reason=f"{rule_id}: {reason}")

    def _execute_pending(self, case: Case, ev: Event) -> str:
        rows = self.conn.execute("SELECT * FROM engine_intents WHERE event_id = ? AND status = 'PENDING' ORDER BY step_seq", (ev.event_id,)).fetchall()
        for i, r in enumerate(rows):
            intent = _intent_from_json(r["intent_json"])
            out: ActionOutcome = self.executor.submit(intent)  # the side effect: always after the ledger write
            ok = out.status in ("OK", "REPLAYED")
            with write_txn(self.conn):
                if not self._executor_ledgers:
                    self.ledger.append(kind="ACTION_RESULT", actor="AGENT", case_id=case.case_id,
                                       payload={"action": intent.action, "key": out.idempotency_key, "status": out.status})
                self.conn.execute("UPDATE engine_intents SET status = ?, outcome_json = ? WHERE case_id = ? AND step_seq = ?",
                                  ("DONE" if ok else out.status, json.dumps({"status": out.status, "key": out.idempotency_key, "error": out.error}),
                                   case.case_id, intent.step_seq))
                if ok and self.registry.get(intent.action).contact:
                    if r["kind"] == "message":
                        self.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id,
                                           payload={"language": intent.args.get("language"), "text": intent.args.get("body")})
                    c = self.repo.load(case.case_id)
                    c.touches += 1
                    self.repo.save(c)
                    if self.contact_budget is not None:
                        self.contact_budget.record(intent.args.get("merchant_id") or case.subject_id, intent.channel or "whatsapp",
                                                   case_id=case.case_id, direction="OUT", proactive=intent.proactive)
            if ok:
                continue
            with write_txn(self.conn):
                self.conn.execute("UPDATE engine_intents SET status = 'SKIPPED' WHERE event_id = ? AND status = 'PENDING'", (ev.event_id,))
            case = self.repo.load(case.case_id)
            rules = [v.rule_id for v in out.violations] or [out.status]
            if out.status == "REFUSED" and r["kind"] == "message" and self._merchant_waiting():
                # a refused REPLY to the merchant's own message: a person takes it, and escalate() sends the fixed hand-over line
                self.escalate(self.repo.load(case.case_id), ["MESSAGE_REFUSED: " + ",".join(rules)], out.violations)
            elif out.status == "REFUSED":
                self.repo.transition(case, "BLOCKED", step=intent.step, reason="refused: " + ",".join(rules))
            elif out.status == "ESCALATE":
                self.escalate(case, rules, out.violations)
            else:
                self._wait(case, None)
            return "STOP"
        return "OK"

    def _merchant_waiting(self) -> bool:
        turn = getattr(self, "_turn", None)
        return bool(self.refused_message_line and turn and turn[0].type in MERCHANT_INITIATED)

    def _handover_if_silent(self, case: Case) -> None:
        """The merchant wrote, this case is going to a person, and nothing has been sent to the merchant since their message: send the pack's fixed
        hand-over line on the channel they used. A system event (nobody asked) sends nothing; a merchant who already got an answer gets no second line."""
        if not self._merchant_waiting() or not case.merchant_id:
            return
        ev, since = self._turn
        answered = self.conn.execute("SELECT 1 FROM ledger l JOIN cases c ON c.case_id = l.case_id WHERE l.seq > ? AND l.kind = 'MESSAGE_SENT' "
                                     "AND c.merchant_id = ? LIMIT 1", (since, case.merchant_id)).fetchone()
        if answered:
            return
        language = ev.payload.get("language") or case.language or "en-IN"
        line = self.refused_message_line(language)
        if not line:
            return
        seq = self.conn.execute("SELECT COALESCE(MAX(step_seq), 0) FROM engine_intents WHERE case_id = ?", (case.case_id,)).fetchone()[0] + 1
        channel = ev.payload.get("channel") or "whatsapp"
        intent = ActionIntent(action=MESSAGE_ACTION, args={"merchant_id": case.merchant_id, "channel": channel, "body": line, "language": language,
                                                           "case_id": case.case_id},
                              case_id=case.case_id, step_seq=seq, playbook_id=case.playbook_id, step=case.step, proactive=False, lane=case.lane,
                              channel=channel, allowlist=(MESSAGE_ACTION,))
        self._send_fixed_line(case, intent, line)

    def _send_fixed_line(self, case: Case, refused: ActionIntent, text: str) -> None:
        """The pack's approved hand-over line, on the same channel as the refused reply. It goes through the executor (and so policy) like any
        message; if it is refused too, nothing is sent and the escalation still happens."""
        intent = replace(refused, args={**refused.args, "body": text})
        out = self.executor.submit(intent)
        if out.status not in ("OK", "REPLAYED"):
            return
        with write_txn(self.conn):
            if not self._executor_ledgers:
                self.ledger.append(kind="ACTION_RESULT", actor="AGENT", case_id=case.case_id,
                                   payload={"action": intent.action, "key": out.idempotency_key, "status": out.status})
            self.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id, payload={"language": intent.args.get("language"), "text": text})
            c = self.repo.load(case.case_id)
            c.touches += 1
            self.repo.save(c)
            if self.contact_budget is not None:
                self.contact_budget.record(intent.args.get("merchant_id") or case.subject_id, intent.channel or "whatsapp",
                                           case_id=case.case_id, direction="OUT", proactive=intent.proactive)

    def _wait(self, case: Case, d: Decision | None) -> None:
        case = self.repo.load(case.case_id)
        if case.state == "ACTIVE":
            case = self.repo.transition(case, "WAITING", reason="waiting")
        hours = getattr(d, "follow_up_hours", None) or getattr(self, "_follow_up", None)
        self._follow_up = None
        if self.timers and hours and case.state == "WAITING":
            from datetime import timedelta
            self.timers.cancel_for_case(case.case_id, kind="follow_up", reason="rescheduled")
            self.timers.schedule(case.case_id, self.clock.now() + timedelta(hours=hours), "follow_up", {})


@dataclass(frozen=True)
class _Resolved:
    name: str


def _json_safe(d: dict) -> dict:
    return json.loads(json.dumps(d, default=str))
