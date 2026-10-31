"""Shared test harness: the REAL components wired together (playbooks, diagnose, engine, policy, executor, mocks).

Import ``World`` from here in any test that needs a whole case to run. `control`, `pay`, `inject` play the outside world
(merchant, courier, customer). Nothing here is production code.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from core.actions.executor import Executor
from core.audit.ledger import Ledger
from core.engine.case_facts import make_facts_fn, replacements_before_case
from core.engine.contact_budget import ContactBudget
from core.engine.diagnose import diagnose, done_check, escalation_reasons
from core.engine.dispatcher import Dispatcher
from core.engine.events import append_event
from core.engine.playbook import load_playbooks
from core.engine.scripted import scripted_decider
from core.engine.timers import TimerService
from core.engine.workflow import Engine
from core.memory.store import connect
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.mocks import create_app
from packs.paytm_merchant.policy.rules import build_engine

ROOT = Path(__file__).resolve().parent.parent
PB_DIR = ROOT / "packs" / "paytm_merchant" / "playbooks"


class World:
    def __init__(self, db_path, clock, script=None, mid="M-0006"):
        self.clock, self.db_path, self.mid = clock, db_path, mid
        self.conn = connect(db_path)
        self.registry = build_registry()
        self.http = TestClient(create_app(db_path, clock))
        self.playbooks = load_playbooks(PB_DIR, self.registry)     # every playbook currently in the pack
        self.budget = ContactBudget(self.conn, clock)
        self.policy = build_engine(self.conn, clock, self.registry, playbook_dir=PB_DIR, contact_budget=self.budget)
        self.ledger = Ledger(self.conn, clock, policy_hash=lambda: self.policy.policy_hash)
        self.executor = Executor(self.conn, clock, self.ledger, self.registry, self.http, self.policy)
        self.reader = self.executor.reader()
        self.timers = TimerService(self.conn, clock, self.ledger)
        self.budget = ContactBudget(self.conn, clock, self.timers)

        def hard(case, ev):
            pb = self.playbooks[case.playbook_id]
            facts = {"touches": case.touches, "intent": ev.payload.get("intent"),
                     "replacements_last_30d": replacements_before_case(self.reader, clock, case)}
            return escalation_reasons(pb, self.reader, case.merchant_id, facts)

        self.engine = Engine(self.conn, clock, self.ledger, self.registry, self.playbooks, self.reader, self.executor, diagnose,
                             scripted_decider(script or {}), hard_escalation=hard, timers=self.timers, done_fn=done_check, contact_budget=self.budget,
                             facts_fn=make_facts_fn(self.conn, self.reader, self.playbooks))
        self.dispatcher = Dispatcher(self.conn, self.engine, self.playbooks)

    def control(self, path, **body):
        r = self.http.post(path.replace("{mid}", self.mid), json=body)
        assert r.status_code == 200, r.text

    def pay(self, key, amount_paise, mid=None):
        r = self.http.post("/mock/payments/simulate", json={"merchant_id": mid or self.mid, "amount_paise": amount_paise}, headers={"Idempotency-Key": key})
        assert r.status_code == 201, r.text
        return r.json()

    def advance_settlement(self, sid, key, to="SETTLED"):
        r = self.http.post(f"/mock/settlements/{sid}/advance", json={"to": to}, headers={"Idempotency-Key": key})
        assert r.status_code == 200, r.text

    def inject(self, event_id, type_, mid=None, payload=None):
        """An event from outside the mocks (the signup funnel, a courier webhook), through the same event log."""
        append_event(self.conn, event_id=event_id, type=type_, payload=payload or {}, ts_sim=self.clock.now_iso(), merchant_id=mid or self.mid)

    def case(self, playbook_id="S1_ACTIVATION", mid=None):
        row = self.conn.execute("SELECT case_id FROM cases WHERE merchant_id=? AND playbook_id=? ORDER BY opened_at DESC, case_id DESC", (mid or self.mid, playbook_id)).fetchone()
        return self.engine.repo.load(row[0])
