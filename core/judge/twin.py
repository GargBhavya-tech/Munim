"""Guard-off twin (#131). A DEMO-ONLY second engine with the guards switched off, run beside the real one so a judge sees what the guards stop.

This module is the ONLY place in the repository where "guards off" exists. There is no flag, parameter or environment variable on the production
path (core/engine, core/actions, core/gate, core/api/app.py): tests/test_judge_twin.py greps the tree to prove it.

Isolation (the Watch out is "twin writing to the shared DB"):
  * the twin owns a SNAPSHOT COPY of the live database (sqlite backup API, source opened query_only) at its own path; it is refused if that path
    is the live file or a hard link / symlink of it;
  * it builds its OWN Stack: own connections, own ledger, own executor + outbox, and its own in-process mock Paytm app on the twin file, so a
    "send message" or "pay" lands in the twin's mock rows and nowhere else. Nothing here is handed a live connection, executor, outbox or HTTP client;
  * ``verify_isolation`` inspects every connection the twin holds (``PRAGMA database_list``) and the mock app's db path, and raises
    TwinIsolationError unless every one is the twin file. It runs at construction and before every message;
  * this module imports no network or LLM client. The one outbound edge is ``client_factory`` (an LLM client for the twin's decisions), injected by
    the caller. The twin never sends anything to a real channel: its executor only knows the twin's own mocks.

What "guards off" switches off, all on the twin's own objects: the policy engine (allow-all), the confidence / injection tiers (always WHITE), the decision guard (a registered action is passed
through as the model proposed it), the grounding gate (the model's claim text is sent as written) and the engine's step allowlist / fix-first
check. What it cannot switch off: the registry. Forbidden actions (approve_kyc, change_bank_details ...) do not exist in code, so even the twin
cannot run them; the model's attempt is recorded as a TWIN_UNKNOWN_ACTION entry instead. Every payload carries BANNER.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from core.actions.types import PolicyDecision
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.api.dev_server import Stack      # imported lazily at use: dev_server imports core.api.app, which imports the judge routes
from core.api.routes_messages import MessageIn, merchant_message
from core.audit.ledger import Ledger
from core.engine.clock import SimClock
from core.engine.guard import GuardResult
from core.engine.workflow import Decision as EngineDecision
from core.gate.pipeline import NO_REPLY, SEND, GateOutcome
from core.gate.tiers import TierResult
from core.memory.store import connect

BANNER = "GUARDS OFF - SIMULATED TWIN"
LABEL = "SIMULATED"
TWIN_POLICY_HASH = "twin-guards-off"


class TwinIsolationError(RuntimeError):
    """The twin holds (or was given) something that is not its own snapshot. Fail closed."""


class GuardsOffPolicy:
    """Allow-all policy. Lives only here; it is installed on the twin's executor and decision guard, never on the real ones."""
    policy_hash = TWIN_POLICY_HASH
    rules: tuple = ()

    def evaluate(self, intent, reader=None) -> PolicyDecision:
        return PolicyDecision("ALLOW", (), TWIN_POLICY_HASH)


class UngroundedGate:
    """The grounding gate, off: the model's own claim text goes out as written, with no source check, number check or compliance line."""
    def run(self, *, claims, book=None, language="en-IN", case_id=None, first_contact=False, merchant_message="", tier=None, style_hint=None,
            rewrite_used=False) -> GateOutcome:
        text = " ".join(c.text for c in claims).strip()
        return GateOutcome(SEND, text, tier="WHITE") if text else GateOutcome(NO_REPLY, None, tier="WHITE")


class _Unavailable:
    """Stands in for the entailment model and translator the (replaced) gate would have used. Calling them is a bug."""
    def label_batch(self, pairs):
        raise RuntimeError("the twin has no grounding model")

    def to_english(self, text, language):
        raise RuntimeError("the twin has no translator")


class UnguardedDecisionGuard:
    """The decision guard, off. A registered action is passed through exactly as the model proposed it; an action that does not exist in the
    registry (every forbidden action) is dropped and recorded, because it cannot be executed by anything."""
    def __init__(self, registry, ledger):
        self.registry, self.ledger = registry, ledger

    def check(self, ctx, td, merchant_message: str = "", sources=None) -> GuardResult:
        action, dropped = None, None
        if td.next_action is not None:
            name, args = td.next_action.name, dict(td.next_action.args)
            if self.registry.has(name):
                action = (name, args)
            else:
                dropped = name
                self.ledger.append(kind="HUMAN_ACTION", actor="TWIN", role="twin", case_id=ctx.case.case_id,
                                   payload={"op": "TWIN_UNKNOWN_ACTION", "action": name, "label": LABEL,
                                            "note": "the model asked for an action that does not exist in the registry; nothing ran"})
        return GuardResult(EngineDecision(action=action), [], [], dropped_action=dropped)


def _db_files(conn: sqlite3.Connection) -> set[str]:
    return {os.path.realpath(r[2]) for r in conn.execute("PRAGMA database_list") if r[2]}


def _same_file(a: Path, b: Path) -> bool:
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    return a.exists() and b.exists() and os.path.samefile(a, b)


def snapshot_copy(live_db: Path, dest: Path) -> None:
    """Consistent copy of ``live_db`` at ``dest`` (sqlite backup API). The source connection is query_only: this never writes the live file."""
    live_db, dest = Path(live_db), Path(dest)
    if _same_file(live_db, dest):
        raise TwinIsolationError("the twin path is the live database")
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(dest) + suffix).unlink(missing_ok=True)
    src = sqlite3.connect(str(live_db), timeout=10)
    src.execute("PRAGMA query_only=ON")
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


class Twin:
    def __init__(self, live_db: str | Path, twin_db: str | Path, now, client_factory: Callable[[Ledger], Any], *, knowledge=None):
        """live_db: only ever READ (to take the snapshot). now: the live clock's current aware datetime. client_factory(ledger): the twin's LLM client."""
        self.live_db, self.twin_db = Path(live_db), Path(twin_db)
        snapshot_copy(self.live_db, self.twin_db)
        if _same_file(self.live_db, self.twin_db):          # re-checked after the copy: a link created in between is refused too
            raise TwinIsolationError("the twin path is the live database")
        self.clock = SimClock(start=now)
        from core.api.dev_server import Stack
        self.stack = Stack(self.twin_db, self.clock)
        self._guards_off(client_factory, knowledge)
        self._state = SimpleNamespace(lock=threading.RLock(), clock=self.clock, db_path=self.twin_db, after_write=self.stack.run)
        self._request = SimpleNamespace(app=SimpleNamespace(state=self._state))
        self.verify_isolation()

    # ------------------------------------------------------------------------------------------------------------------- guards off
    def _guards_off(self, client_factory, knowledge) -> None:
        from core.engine.assembly import build_turn_agent, hard_escalation, wire
        from core.engine.handoff import HandoffService
        s = self.stack
        off = GuardsOffPolicy()
        s.policy = s.executor.policy = off
        client = client_factory(s.ledger)
        agent = build_turn_agent(s.engine, policy=off, client=client, nli=_Unavailable(), translator=_Unavailable(), knowledge=knowledge,
                                 executor=s.executor)
        agent.guard = UnguardedDecisionGuard(s.registry, s.ledger)
        agent.gate = UngroundedGate()
        agent._tier = lambda *a, **k: TierResult("WHITE")          # the confidence/injection tiers (BLUE clarify, RED hand-off): off
        reg = s.registry
        s.engine.case_manager.intent_allowed = lambda action, allowlist, lanes, **kw: SimpleNamespace(
            ok=reg.has(action), rule_id=None if reg.has(action) else "UNKNOWN-ACTION", reason=None)   # step allowlist and fix-first: off
        s.engine.hard_escalation = hard_escalation(s.playbooks, s.reader, s.clock)
        wire(s.engine, agent, handoffs=HandoffService(s.conn, s.clock, s.ledger, executor=s.executor, on_return=s.engine.handle_event))

    # ------------------------------------------------------------------------------------------------------------------- isolation
    def holders(self) -> dict[str, sqlite3.Connection]:
        s = self.stack
        return {"stack.conn": s.conn, "executor.conn": s.executor.conn, "outbox.conn": s.executor.outbox.conn, "ledger.conn": s.ledger.conn,
                "engine.conn": s.engine.conn}

    def verify_isolation(self) -> None:
        want = os.path.realpath(self.twin_db)
        live = os.path.realpath(self.live_db)
        if want == live or _same_file(self.live_db, self.twin_db):
            raise TwinIsolationError("the twin path is the live database")
        for name, conn in self.holders().items():
            files = _db_files(conn)
            if files != {want}:
                raise TwinIsolationError(f"{name} is attached to {sorted(files)}, not the twin snapshot")
        if os.path.realpath(self.stack.mock_app.state.ctx.db_path) != want:
            raise TwinIsolationError("the twin's mock Paytm app is not on the twin snapshot")
        if self.stack.executor.http.app is not self.stack.mock_app:
            raise TwinIsolationError("the twin's executor does not talk to the twin's own mock app")

    # ------------------------------------------------------------------------------------------------------------------- use
    def handle(self, merchant_id: str, text: str, language: str | None = None) -> dict:
        """One merchant message through the twin, by the same handler the real engine uses, on the twin's own state."""
        self.verify_isolation()
        with self._state.lock:
            after = self.stack.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
            out = merchant_message(merchant_id, MessageIn(text=text, language=language, channel="app"), self._request)
            acts = self.stack.conn.execute(
                "SELECT json_extract(payload_json,'$.action'), json_extract(payload_json,'$.status'), json_extract(payload_json,'$.cls') FROM ledger "
                "WHERE seq > ? AND kind = 'ACTION_RESULT' ORDER BY seq", (after,)).fetchall()
            blocked = [r[0] for r in self.stack.conn.execute(
                "SELECT json_extract(payload_json,'$.action') FROM ledger WHERE seq > ? AND kind = 'HUMAN_ACTION' AND actor = 'TWIN' "
                "AND json_extract(payload_json,'$.op') = 'TWIN_UNKNOWN_ACTION'", (after,))]
        self.verify_isolation()
        return {**twin_header(), **out, "actions": [{"action": a, "status": st, "cls": c} for a, st, c in acts], "nonexistent_actions_requested": blocked}

    def close(self) -> None:
        for conn in {id(c): c for c in self.holders().values()}.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass


def twin_header() -> dict:
    """Part of EVERY twin payload, errors included."""
    return {"banner": BANNER, "label": LABEL, "twin": True, "guards": "OFF"}
