"""Test harness for the consumer pack (#89): the REAL core (engine, policy engine, executor, ledger) around the consumer pack's own mocks,
registry, fields, rules and playbooks. Nothing from the merchant pack. Not collected as tests (file name)."""
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import packs.paytm_consumer.fields  # noqa: F401  registers upi.* and crefund.* with the core
from core.actions.executor import Executor
from core.audit.ledger import Ledger, verify_chain
from core.engine.case_facts import make_facts_fn
from core.engine.contact_budget import ContactBudget
from core.engine.diagnose import diagnose, done_check, escalation_reasons
from core.engine.dispatcher import Dispatcher
from core.engine.events import append_event
from core.engine.fields import build_namespace
from core.engine.intents import detect_intent
from core.engine.playbook import load_playbooks
from core.engine.scripted import scripted_decider
from core.engine.timers import TimerService
from core.engine.workflow import Engine
from core.memory.store import connect
from packs.paytm_consumer.actions import build_registry
from packs.paytm_consumer.mocks import create_app
from packs.paytm_consumer.policy import build_engine

PACK = Path(__file__).resolve().parent.parent / "packs" / "paytm_consumer"
INTENTS = PACK / "intents.yaml"
DISPUTE_MSG = "We have raised a dispute for this payment with the bank. A stuck debit is usually reversed by the bank; we will keep you updated."
REFUND_MSG = "Your refund is being processed by the bank. We are tracking it and will tell you when it is credited."


def _stuck_txn(ctx, reader):
    txns = reader("get_upi_transactions", {"merchant_id": ctx.case.merchant_id})["transactions"]
    return next((t["txn_id"] for t in txns if t["status"] == "DEBITED_PENDING"), None)


class ConsumerWorld:
    def __init__(self, db_path, clock, mid="U-0001"):
        self.clock, self.db_path, self.mid = clock, db_path, mid
        self.conn = connect(db_path)
        self.registry = build_registry()
        self.http = TestClient(create_app(db_path, clock))
        self.playbooks = load_playbooks(PACK / "playbooks", self.registry, kb_scopes=set())
        self.policy = build_engine(self.conn, clock, self.registry, playbook_dir=PACK / "playbooks")
        self.ledger = Ledger(self.conn, clock, policy_hash=lambda: self.policy.policy_hash)
        self.executor = Executor(self.conn, clock, self.ledger, self.registry, self.http, self.policy)
        self.reader = self.executor.reader()
        self.timers = TimerService(self.conn, clock, self.ledger)
        self.budget = ContactBudget(self.conn, clock, self.timers)
        script = {
            ("C11_UPI_DEBITED_FAILED", "dispute"): self._dispute,
            ("C12_REFUND_NOT_RECEIVED", "status"): lambda ctx: _decision(None, REFUND_MSG),
        }

        def hard(case, ev):
            return escalation_reasons(self.playbooks[case.playbook_id], self.reader, case.merchant_id, {"touches": case.touches, "intent": ev.payload.get("intent")})

        self.engine = Engine(self.conn, clock, self.ledger, self.registry, self.playbooks, self.reader, self.executor, diagnose, scripted_decider(script),
                             hard_escalation=hard, timers=self.timers, done_fn=done_check, contact_budget=self.budget,
                             facts_fn=make_facts_fn(self.conn, self.reader, self.playbooks))
        self.dispatcher = Dispatcher(self.conn, self.engine, self.playbooks, message_fallback="")

    def _dispute(self, ctx):
        from core.engine.workflow import Decision
        txn = _stuck_txn(ctx, self.reader)
        return _decision(("raise_upi_dispute", {"merchant_id": ctx.case.merchant_id, "txn_id": txn}), DISPUTE_MSG) if txn else Decision()

    def control(self, path, **body):
        r = self.http.post(path.replace("{mid}", self.mid), json=body)
        assert r.status_code == 200, r.text

    def say(self, eid, text, mid=None, playbook_id=None):
        """The consumer writes. Opens the case of the message's intent (or the scenario's playbook) if none of it is open."""
        mid = mid or self.mid
        hits = detect_intent(text, None, INTENTS)
        intent = hits[0][0] if hits else None
        from yaml import safe_load
        mapping = {k: v["playbook"] for k, v in safe_load(INTENTS.read_text(encoding="utf-8"))["intents"].items()}
        pid = mapping.get(intent) if mapping.get(intent) in self.playbooks else playbook_id
        if pid and not any(c.playbook_id == pid for c in self.engine.repo.open_cases(mid)):
            self.engine.open_case(mid, pid)
        append_event(self.conn, event_id=eid, type="MERCHANT_MESSAGE", payload={"text": text, "intent": intent, "channel": "app"},
                     ts_sim=self.clock.now_iso(), merchant_id=mid)
        self.dispatcher.run()

    def case(self, pid, mid=None):
        row = self.conn.execute("SELECT case_id FROM cases WHERE merchant_id=? AND playbook_id=? ORDER BY opened_at DESC, case_id DESC", (mid or self.mid, pid)).fetchone()
        return self.engine.repo.load(row[0]) if row else None


def _decision(action, message):
    from core.engine.workflow import Decision
    return Decision(action=action, message_en=message, channel="app")


@dataclass
class Result:
    case_state: str | None
    escalated: bool
    done_fields: dict
    executed: dict = field(default_factory=dict)
    touches: int = 0
    ledger_ok: bool = True


def run_consumer_scenario(w: ConsumerWorld, sc) -> Result:
    w.mid = mid = sc.persona.merchant_id
    for n, st in enumerate(sc.steps):
        if st.after_hours:
            w.clock.advance(timedelta(hours=st.after_hours))
        if st.merchant_says is not None:
            w.say(f"{sc.id}-{n}", st.merchant_says, playbook_id=sc.playbook_id)
        elif st.control:
            w.control(st.control["path"], **st.control.get("body", {}))
        elif st.event:
            append_event(w.conn, event_id=f"{sc.id}-{n}", type=st.event, payload={}, ts_sim=w.clock.now_iso(), merchant_id=mid)
            w.dispatcher.run()
    case = w.case(sc.playbook_id)
    reads = sorted({r for pb in w.playbooks.values() for r in pb.required_reads})
    ns = build_namespace(w.reader, mid, reads, {})
    executed = {}
    for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'ACTION_RESULT' ORDER BY seq"):
        p = json.loads(r[0])
        if p.get("status") == "OK" and p.get("cls", "R3") != "R0":
            executed[p["action"]] = executed.get(p["action"], 0) + 1
    esc = bool(w.conn.execute("SELECT 1 FROM ledger WHERE kind='ESCALATION' AND case_id=?", (case.case_id,)).fetchone()) if case else False
    return Result(case.state if case else None, esc, {f: ns.get(f) for f in sc.expected.done_fields}, executed, case.touches if case else 0,
                  verify_chain(w.conn).ok)
