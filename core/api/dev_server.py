"""Local dev stack for the dashboard: seeded DB, in-process mocks, real policy engine, real executor, real playbooks.

    python -m core.api.dev_server --port 8000

Decisions are the scripted (non-LLM) S1 decider, so the UI has real cases, ledger entries and actions to show. The demo
controls (clock, pay, chaos, kill switch) drive the same stack; after each, the dispatcher runs so cases move.

    python -m core.api.dev_server --llm

``--llm`` replaces the scripted decider with the production turn agent (core.engine.assembly): Sarvam for decisions, rendering and
back-translation, the real entailment model, hybrid KB retrieval, hard triggers and full handoff packets. It needs SARVAM_API_KEY in .env
and the requirements-ml.txt models. Merchants then talk through POST /api/merchants/{mid}/messages (text) or /ws/voice.
"""
from __future__ import annotations

import argparse
import secrets
import tempfile
from datetime import timedelta
from pathlib import Path

import yaml



from core.actions.executor import Executor
from core.api.app import create_api
from core.audit.ledger import Ledger
from core.engine.case_facts import make_facts_fn, replacements_before_case
from core.engine.clock import SimClock
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
from packs.paytm_merchant.policy.rules import build_engine

# The dev server runs the mock world in-process. tests/test_executor.py allow-lists exactly this file for that reason.
from fastapi.testclient import TestClient as _Client
from packs.paytm_merchant.mocks import create_app as create_mocks

ROOT = Path(__file__).resolve().parent.parent.parent
PB_DIR = ROOT / "packs" / "paytm_merchant" / "playbooks"
MID = "M-0006"
S1_SCRIPT = {
    ("S1_ACTIVATION", "kyc"): {"action": "send_kyc_resume_link", "args": {"merchant_id": MID}, "channel": "app"},
    ("S1_ACTIVATION", "bank"): ("retrigger_bank_verification", {"merchant_id": MID}),
    ("S1_ACTIVATION", "device"): ("create_device_replacement", {"merchant_id": MID, "reason": "NO_ANNOUNCEMENT"}),
}


class Stack:
    def __init__(self, db_path, clock: SimClock, script=None):
        self.db_path, self.clock = db_path, clock
        self.conn = connect(db_path)
        self.registry = build_registry()
        self.mock_app = create_mocks(db_path, clock)
        self.http = _Client(self.mock_app)
        self.playbooks = load_playbooks(PB_DIR, self.registry)
        budget = ContactBudget(self.conn, clock)
        self.policy = build_engine(self.conn, clock, self.registry, playbook_dir=PB_DIR, contact_budget=budget)
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
                             scripted_decider(script or S1_SCRIPT), hard_escalation=hard, timers=self.timers, done_fn=done_check, contact_budget=self.budget,
                             facts_fn=make_facts_fn(self.conn, self.reader, self.playbooks))
        self.dispatcher = Dispatcher(self.conn, self.engine, self.playbooks)
        from jobs.incidents import IncidentManager
        self.incidents = IncidentManager(self.conn, clock, self.ledger, self.executor, self.engine.case_manager, self.engine.repo)
        self.engine.hold_fn = self.incidents.hold_note

    def inject(self, event_id, type_, mid=MID):
        append_event(self.conn, event_id=event_id, type=type_, payload={}, ts_sim=self.clock.now_iso(), merchant_id=mid)

    def run(self):
        self.incidents.run()                 # area incidents first: held merchants' fault cases park before they act (#88)
        return self.dispatcher.run()

    def control(self, path, **body):
        r = self.http.post(path, json=body)
        assert r.status_code == 200, r.text


def seed_demo_cases(stack: Stack) -> None:
    """Replay the first half of the S1 flow for M-0006 so the dashboard opens on a live case with a ledger and actions."""
    stack.inject("DEMO-STALL", "SIGNUP_STALLED")
    stack.run()
    stack.clock.advance(timedelta(hours=2))
    stack.control(f"/mock/control/kyc/{MID}", status="APPROVED")
    stack.inject("DEMO-KYC", "KYC_SUBMITTED")
    stack.run()


def attach_llm(stack: Stack, *, client=None, nli=None, translator=None, knowledge=None, embedder=None, reranker=None):
    """Swap the scripted decider for the production turn agent. Providers default to the real ones (Sarvam, entailment model, KB)."""
    from core.engine.assembly import build_turn_agent, hard_escalation, wire
    from core.engine.handoff import HandoffService
    if client is None or translator is None:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        from core.llm.client import build_client
        from core.llm.sarvam_http import SarvamHTTP
        from core.gate.faithfulness import SarvamTranslator
        client = client or build_client(stack.ledger)
        translator = translator or SarvamTranslator(SarvamHTTP())
    if nli is None:
        from core.gate.nli import EntailmentModel
        nli = EntailmentModel()
    if knowledge is None:
        from core.engine.knowledge_layer import KnowledgeLayer
        from core.gate.embedder import SentenceTransformerEmbedder
        from core.gate.retrieval import CrossEncoderReranker, Retriever, index_vectors
        from core.memory.knowledge import index_docs, load_kb_dir
        index_docs(stack.conn, load_kb_dir(ROOT / "packs" / "paytm_merchant" / "kb"))
        embedder = embedder or SentenceTransformerEmbedder()
        index_vectors(stack.conn, embedder)
        knowledge = KnowledgeLayer(Retriever(stack.conn, embedder, reranker or CrossEncoderReranker()), conn=stack.conn, clock=stack.clock)
    stack.engine.hard_escalation = hard_escalation(stack.playbooks, stack.reader, stack.clock)
    from packs.paytm_merchant import scam_shield
    agent = build_turn_agent(stack.engine, policy=stack.policy, client=client, nli=nli, translator=translator, knowledge=knowledge,
                             executor=stack.executor,
                             aliases=yaml.safe_load((ROOT / "packs" / "paytm_merchant" / "gate_aliases.yaml").read_text(encoding="utf-8")),
                             handlers={scam_shield.PLAYBOOK_ID: scam_shield.make_handler(stack.conn, stack.reader, stack.clock, stack.ledger)})
    return wire(stack.engine, agent, handoffs=HandoffService(stack.conn, stack.clock, stack.ledger, executor=stack.executor,
                                                             on_return=stack.engine.handle_event))


class SharedModels:
    """The models a live stack needs, loaded ONCE and shared by the main stack, every Break-Munim sandbox and the guard-off twin."""

    def __init__(self, offline: bool = False):
        """``offline``: the rule-based stand-in for Sarvam (core.llm.offline). The entailment model, retriever, guard, policy, executor and gate are the real ones."""
        from core.gate.embedder import SentenceTransformerEmbedder
        from core.gate.nli import EntailmentModel
        from core.gate.retrieval import CrossEncoderReranker
        self.offline = offline
        if offline:
            from core.llm.offline import Hinglish, OfflineTranslator
            self.hinglish = Hinglish()
            self.nli, self.translator = EntailmentModel(), OfflineTranslator(self.hinglish)
        else:
            from core.gate.faithfulness import SarvamTranslator
            from core.llm.sarvam_http import SarvamHTTP
            self.nli, self.translator = EntailmentModel(), SarvamTranslator(SarvamHTTP())
        self.embedder, self.reranker = SentenceTransformerEmbedder(), CrossEncoderReranker()

    def client_for(self, ledger):
        """A model client for ``ledger``: Sarvam, or the offline stand-in."""
        if self.offline:
            from core.llm.offline import offline_client
            return offline_client(ledger, self.hinglish)
        from core.llm.client import build_client
        return build_client(ledger)

    def attach(self, stack: Stack):
        return attach_llm(stack, client=self.client_for(stack.ledger) if self.offline else None, nli=self.nli, translator=self.translator,
                          embedder=self.embedder, reranker=self.reranker)


def enable_live_extras(app, db_path: Path, clock, work_dir: Path, *, attach_sandbox, client_factory) -> None:
    """Judge Mode's two signature features, which need a model and so exist only on the live (--llm) stack:
    * Break Munim (#132): every player gets a sandbox copy of ``db_path``; ``attach_sandbox(stack)`` puts the live turn agent on it.
    * the guard-off twin (#131): ``client_factory(ledger)`` gives it a model client; it runs on its own snapshot DB with no outbound comms.
    Secrets are per process unless MUNIM_ARENA_SECRET / MUNIM_ARENA_ADMIN_TOKEN are set; the admin token (kill switch) is printed once."""
    import os
    from core.judge.arena import Arena
    app.state.arena = Arena(db_path, work_dir / "arena", clock, attach_sandbox, secret=os.environ.get("MUNIM_ARENA_SECRET") or secrets.token_hex(16))
    app.state.arena_admin_token = os.environ.get("MUNIM_ARENA_ADMIN_TOKEN") or secrets.token_hex(8)
    app.state.twin_enabled = True
    app.state.twin_dir = work_dir / "twins"
    app.state.twin_client_factory = client_factory
    print(f"[dev_server] Break Munim and the guard-off twin are on. Arena admin token (kill switch): {app.state.arena_admin_token}", flush=True)


WARMUP_TEXT = "my settlement has not come"
WARMUP_MERCHANT = "M-0001"


def warm_up(attach, db_path: Path) -> dict:
    """One real turn through the production path BEFORE any judge types, so the entailment model, retriever, reranker and Sarvam connection are loaded and
    warm (the first live turn otherwise takes 10 s or more). It runs on a throwaway COPY of the database with its own stack and clock: the demo database,
    ledger and merchants are untouched. It goes through the same turn agent, gate, policy and executor as a judge's message; nothing is stubbed.
    ``attach(stack)`` puts the live turn agent on the stack (``SharedModels.attach``, so the models it loads are the ones the demo then uses)."""
    import time
    from fastapi.testclient import TestClient
    from core.memory.store import copy_db
    copy = Path(tempfile.mkdtemp(prefix="munim-warm-")) / "munim.db"
    copy_db(db_path, copy)
    clock = SimClock()
    stack = Stack(copy, clock)
    attach(stack)
    app = create_api(copy, clock, stack.executor, stack.http, registry=stack.registry, after_write=stack.run, on_approve=stack.executor.submit)
    t0 = time.perf_counter()
    r = TestClient(app).post(f"/api/merchants/{WARMUP_MERCHANT}/messages", json={"text": WARMUP_TEXT, "language": "en-IN"})
    down = llm_failed(stack.conn)
    out = {"ok": r.status_code == 200 and not down, "status": r.status_code, "seconds": round(time.perf_counter() - t0, 1), "llm_ok": not down}
    out["replies"] = len(r.json().get("replies", [])) if r.status_code == 200 else 0
    return out


def llm_failed(conn) -> bool:
    """True if the turn was handed to a person because the model could not be reached (an ESCALATION for UPSTREAM_UNAVAILABLE or TOOL_FAILURE: llm).
    That reply is the fixed hand-over line, not an answer: a warm-up that counted it as success would hide a missing key until a judge typed."""
    return any("UPSTREAM_UNAVAILABLE" in r[0] and "llm" in r[0] for r in conn.execute("SELECT payload_json FROM ledger WHERE kind = 'ESCALATION'"))


def build(db_path: Path | None = None, seed_seed: int = 42, demo: bool = True, llm: bool = False, offline: bool = False):
    from data.seed import seed
    if db_path is None:
        db_path = Path(tempfile.mkdtemp(prefix="munim-dev-")) / "munim.db"
    if not Path(db_path).exists():
        seed(db_path, seed_seed)
    clock = SimClock()
    stack = Stack(db_path, clock)
    if demo:
        seed_demo_cases(stack)
    shared = None
    if llm or offline:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        shared = SharedModels(offline=offline)
        shared.attach(stack)
    app = create_api(db_path, clock, stack.executor, stack.http, registry=stack.registry, after_write=stack.run, on_approve=stack.executor.submit)
    if not app.state.phone_secret:              # the demo stack: a judge's phone QR must work without setup; production still needs MUNIM_PHONE_SECRET
        app.state.phone_secret = secrets.token_hex(16)
    if shared is not None:
        app.state.warm_attach = shared.attach          # main() warms the models with one real turn on a throwaway copy before serving
        enable_live_extras(app, Path(db_path), clock, Path(tempfile.mkdtemp(prefix="munim-live-")), attach_sandbox=shared.attach,
                           client_factory=shared.client_for)
    return app, stack


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=None, help="path to a SQLite file (seeded if missing); default: a fresh temp DB")
    ap.add_argument("--no-demo", action="store_true", help="do not replay the demo S1 flow")
    ap.add_argument("--llm", action="store_true", help="decide with the production turn agent (needs SARVAM_API_KEY and the ML models)")
    ap.add_argument("--offline", action="store_true", help="the production turn agent with a rule-based stand-in for Sarvam (no key, no network): see core/llm/offline.py")
    ap.add_argument("--no-warmup", action="store_true", help="with --llm: skip the start-up turn that loads the models and makes the first Sarvam call")
    a = ap.parse_args()
    app, _ = build(Path(a.db) if a.db else None, demo=not a.no_demo, llm=a.llm, offline=a.offline)
    if a.offline:
        print("[dev_server] OFFLINE MODE: a rule-based stand-in replaces Sarvam. Policy, executor, grounding gate and ledger are the real ones. English and Hinglish only.", flush=True)
    attach = getattr(app.state, "warm_attach", None)
    if attach is not None and not a.no_warmup:
        print("[dev_server] warming up: one real turn on a throwaway copy, so the first judge does not wait...", flush=True)
        import os
        if not a.offline and not os.environ.get("SARVAM_API_KEY"):
            print("[dev_server] WARNING: SARVAM_API_KEY is empty (put it in .env). Every live turn will be handed to a person with the fixed hand-over line.", flush=True)
        try:
            r = warm_up(attach, Path(app.state.db_path))
            if not r["llm_ok"]:
                print(f"[dev_server] WARM-UP FAILED: the model did not answer (the one reply was the hand-over line). Check SARVAM_API_KEY and the network. {r['seconds']} s.", flush=True)
            else:
                print(f"[dev_server] warm-up {'done' if r['ok'] else 'FAILED'} in {r['seconds']} s (HTTP {r['status']}, {r['replies']} reply). Ready.", flush=True)
        except Exception as e:                         # a warm-up problem must never stop the demo from starting
            print(f"[dev_server] warm-up failed ({type(e).__name__}: {str(e)[:200]}); serving anyway, the first turn will be slow.", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
