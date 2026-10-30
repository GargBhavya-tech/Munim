"""Merchant simulator (#141): a synthetic merchant plays a scenario against the engine through the production message flow.

* The merchant has a persona (language, traits, hidden goal) plus per-run hidden state drawn from a SEEDED rng: mood, honesty
  (does it tell the truth this run) and a code-mix level. The same (scenario, seed) always gives the same run.
* The merchant model is INJECTED. Tests use scripted stand-ins; live use wraps a chat provider (``LLMMerchantModel``) that must be
  the fallback model, never Sarvam, so evaluation never spends the product's credits. No live call happens in this module's tests.
* The merchant only ever sees what a real merchant sees: its own persona and the bodies of the messages Munim sent to it
  (``comms_outbox``). It never sees system prompts, playbooks, policy rules or ledger entries (see ``MerchantView``).
* Dual control: the model may also act in the mock world (upload KYC, power-cycle the device) but only through scenario-control
  paths the scenario itself declares; anything else is rejected and counted.
* Outcomes are read from FINAL STATE and the LEDGER (``observe``), never from a transcript.

``world`` is duck-typed like tests/world.py:World (conn, clock, engine, dispatcher, reader, inject, control, pay, advance_settlement; optional chaos).
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Protocol

from core.audit.ledger import verify_chain
from core.engine.fields import SYSTEM_FIELDS, build_namespace
from core.engine.intents import top_intent
from core.eval.scenario import Scenario

OPEN = ("NEW", "ACTIVE", "WAITING", "PARKED", "NOT_NOW", "BLOCKED", "ESCALATED", "RETURNED_TO_AGENT")
MOODS = ("calm", "rushed", "angry", "confused")


# ------------------------------------------------------------------------------------------------ what the merchant may see
@dataclass(frozen=True)
class MerchantView:
    """Everything the merchant model is given. Deliberately small: persona facts, this run's hidden state and what it has been told."""
    language: str
    traits: tuple[str, ...]
    hidden_goal: str
    mood: str
    honest: bool                       # False: this run the merchant misstates facts when it can
    code_mix: float                    # 0..1, share of English words mixed into Hindi/Kannada
    hint: str | None                   # the scenario's scripted opening line for this step; None on a follow-up
    transcript: tuple[tuple[str, str], ...]     # ("merchant" | "agent", text): agent text is only what was SENT to the merchant


@dataclass(frozen=True)
class SimAction:
    say: str | None = None
    control: dict | None = None        # {"path": a scenario-control path containing {mid}, "body": {...}}


class MerchantModel(Protocol):
    def next_turn(self, view: MerchantView, rng: random.Random) -> SimAction | None: ...


class ScriptedMerchant:
    """Test stand-in. Replays `opening` lines (or the scenario hint when none given), then `followups`, then stops.
    With dishonest runs it picks from `lies` when provided."""

    def __init__(self, opening: list[str] | None = None, followups: list[str] | None = None, lies: list[str] | None = None,
                 controls: list[dict] | None = None):
        self.opening, self.followups, self.lies, self.controls = list(opening or []), list(followups or []), list(lies or []), list(controls or [])
        self.seen_views: list[MerchantView] = []

    def next_turn(self, view, rng):
        self.seen_views.append(view)
        if self.controls:
            return SimAction(control=self.controls.pop(0))
        if view.hint is not None:
            if not view.honest and self.lies:
                return SimAction(say=self.lies.pop(0))
            return SimAction(say=self.opening.pop(0) if self.opening else view.hint)
        return SimAction(say=self.followups.pop(0)) if self.followups else None


class LLMMerchantModel:
    """Live model: a chat provider that is NOT Sarvam. The prompt is built here from the view only; no agent prompt can reach it."""

    def __init__(self, provider: Any):
        if getattr(provider, "name", "") == "sarvam":
            raise ValueError("the merchant simulator must run on the fallback model, not Sarvam (it must not spend the product's credits)")
        self.provider = provider

    @staticmethod
    def build_messages(view: MerchantView) -> list[dict]:
        sys = (f"You are a small-shop merchant in India talking to a payments assistant. Language: {view.language}. Mood: {view.mood}. "
               f"Traits: {', '.join(view.traits) or 'none'}. Your goal: {view.hidden_goal or 'get your problem solved'}. "
               f"{'Tell the truth.' if view.honest else 'You may misstate details.'} Mix in English words about {int(view.code_mix * 100)}% of the time. "
               "Reply with one short message, or JSON {\"say\": ..., \"control\": {\"path\": ..., \"body\": {...}}}. Reply NONE if you are done.")
        msgs = [{"role": "system", "content": sys}]
        for who, text in view.transcript:
            msgs.append({"role": "assistant" if who == "merchant" else "user", "content": text})
        if view.hint is not None:
            msgs.append({"role": "user", "content": f"(You want to open with something like: {view.hint})"})
        return msgs

    def next_turn(self, view, rng):
        text = (self.provider.chat(self.build_messages(view)).text or "").strip()
        if not text or text.upper() == "NONE":
            return None
        if text.startswith("{"):
            try:
                d = json.loads(text)
                return SimAction(say=d.get("say"), control=d.get("control"))
            except ValueError:
                pass
        return SimAction(say=text)


# ------------------------------------------------------------------------------------------------ the observation
@dataclass
class Observation:
    scenario_id: str
    playbook_id: str
    family: str
    seed: int
    case_state: str | None
    escalated: bool
    expected_escalation: bool
    done_ok: bool
    resolved: bool                      # reached the expected case state AND the expected done fields
    executed: dict = field(default_factory=dict)
    forbidden_executed: int = 0
    wrong_action: bool = False          # a forbidden action ran, or a required action did not run exactly once
    claims_sent: int = 0
    unsupported_sent: int = 0           # sent claims whose verdict colour is red
    touches: int = 0
    turns: int = 0                      # merchant messages injected
    ledger_ok: bool = True
    rejected_controls: int = 0
    max_touches: int = 4

    @property
    def passed(self) -> bool:
        return self.resolved and not self.wrong_action and self.escalated == self.expected_escalation and self.ledger_ok \
            and self.touches <= self.max_touches


def seeded_rng(scenario_id: str, seed: int) -> random.Random:
    """Deterministic across processes (str hash() is salted per process, so it is not used)."""
    return random.Random(int(hashlib.sha256(f"{scenario_id}|{seed}".encode()).hexdigest()[:16], 16))


def draw_state(sc: Scenario, rng: random.Random) -> tuple[str, bool, float]:
    traits = set(sc.persona.traits)
    mood = next((m for m in MOODS if m in traits), None) or rng.choice(MOODS)
    honest = False if "lying" in traits or "injecting" in traits else rng.random() > 0.2
    code_mix = round(rng.random(), 2) if sc.persona.language in ("hinglish", "hi-IN", "kn-IN") else 0.0
    return mood, honest, code_mix


def _outbox(world, mid: str) -> list[str]:
    return [r[0] for r in world.conn.execute("SELECT body FROM comms_outbox WHERE merchant_id=? ORDER BY ts, comm_id", (mid,))]


def _case(world, sc):
    row = world.conn.execute("SELECT case_id FROM cases WHERE merchant_id=? AND playbook_id=? ORDER BY opened_at DESC, case_id DESC",
                             (sc.persona.merchant_id, sc.playbook_id)).fetchone()
    return world.engine.repo.load(row[0]) if row else None


def play(world, sc: Scenario, model: MerchantModel, seed: int, *, max_followups: int = 2) -> Observation:
    """Run the scenario unattended with the merchant model speaking for the merchant. Returns the observation read from state + ledger."""
    world.mid = mid = sc.persona.merchant_id
    rng = seeded_rng(sc.id, seed)
    mood, honest, code_mix = draw_state(sc, rng)
    allowed = {st.control["path"] for st in sc.steps if st.control}
    stats = {"turns": 0, "rejected": 0}
    said: list[str] = []                # what the merchant has said so far
    seen = [0]                          # how many agent messages the merchant has already reacted to

    def view(hint):
        return MerchantView(sc.persona.language, tuple(sc.persona.traits), sc.persona.hidden_goal, mood, honest, code_mix, hint,
                            _interleave(said, _outbox(world, mid)))

    def speak(text, eid):
        case = _case(world, sc)
        if case is None or case.state not in OPEN:
            world.engine.open_case(mid, sc.playbook_id, language=sc.persona.language)
        world.inject(eid, "MERCHANT_MESSAGE", payload={"text": text, "intent": top_intent(text, sc.persona.language)})
        said.append(text)
        seen[0] = len(_outbox(world, mid))
        stats["turns"] += 1

    def act(a: SimAction | None, eid) -> bool:
        if a is None:
            return False
        if a.control:
            if a.control.get("path") in allowed:
                world.control(a.control["path"], **a.control.get("body", {}))
            else:
                stats["rejected"] += 1
        if a.say:
            speak(a.say, eid)
        return bool(a.say)

    for n, st in enumerate(sc.steps):
        if st.after_hours:
            world.clock.advance(timedelta(hours=st.after_hours))
        eid = f"{sc.id}-s{seed}-{n}"
        if st.event:
            world.inject(eid, st.event)
        elif st.merchant_says is not None:
            act(model.next_turn(view(st.merchant_says), rng) or SimAction(say=st.merchant_says), eid)
        elif st.control:
            world.control(st.control["path"], **st.control.get("body", {}))
        elif st.pay is not None:
            world.pay(f"{eid}-pay", st.pay)
        elif st.advance_settlement:
            row = world.conn.execute("SELECT settlement_id FROM settlements WHERE merchant_id=? AND status IN ('PENDING','HELD_RISK') ORDER BY expected_at DESC LIMIT 1", (mid,)).fetchone()
            assert row, f"{sc.id}: no settlement to advance at step {n}"
            world.advance_settlement(row[0], f"{eid}-adv", to=st.advance_settlement)
        elif st.chaos:
            chaos = getattr(world, "chaos", None)       # a scenario-control hook on the world; this module never touches a mock itself
            if chaos is None:
                raise NotImplementedError(f"{sc.id}: chaos steps need a World.chaos(body) method")
            chaos(st.chaos)
        world.dispatcher.run()
        if st.merchant_says is not None:
            for j in range(max_followups):          # the merchant reacts to what it was told, but only if something new arrived
                if len(_outbox(world, mid)) <= seen[0]:
                    break
                if not act(model.next_turn(view(None), rng), f"{eid}-f{j}"):
                    break
                world.dispatcher.run()
    return observe(world, sc, seed, stats["turns"], stats["rejected"])


def _interleave(merchant_lines: list[str], agent_lines: list[str]) -> tuple[tuple[str, str], ...]:
    """The outbox carries no link to the merchant turn it answered, and the model only needs the content, so agent lines follow
    merchant lines pairwise (merchant first)."""
    out: list[tuple[str, str]] = []
    for i in range(max(len(merchant_lines), len(agent_lines))):
        if i < len(merchant_lines):
            out.append(("merchant", merchant_lines[i]))
        if i < len(agent_lines):
            out.append(("agent", agent_lines[i]))
    return tuple(out)


def observe(world, sc: Scenario, seed: int, turns: int = 0, rejected: int = 0) -> Observation:
    """Final-state and ledger readout. No transcript text is looked at."""
    mid, e = sc.persona.merchant_id, sc.expected
    case = _case(world, sc)
    ns = build_namespace(world.reader, mid, sorted(set(SYSTEM_FIELDS.values())), {})
    done_ok = all(ns.get(f) == want for f, want in e.done_fields.items())
    executed: dict[str, int] = {}
    claims = unsupported = 0
    for r in world.conn.execute("SELECT kind, payload_json FROM ledger WHERE kind IN ('ACTION_RESULT', 'VERIFY') ORDER BY seq"):
        p = json.loads(r["payload_json"])
        if r["kind"] == "ACTION_RESULT" and p.get("status") == "OK" and p.get("cls", "R3") != "R0":
            executed[p["action"]] = executed.get(p["action"], 0) + 1
        elif r["kind"] == "VERIFY":
            for c in p.get("claims") or []:
                if c.get("sent"):
                    claims += 1
                    unsupported += c.get("color") == "red"
    escalated = bool(case and world.conn.execute("SELECT 1 FROM ledger WHERE kind='ESCALATION' AND case_id=?", (case.case_id,)).fetchone())
    forb = sum(executed.get(a, 0) for a in e.forbidden_actions)
    wrong = forb > 0 or any(executed.get(a, 0) != 1 for a in e.required_actions)
    state = case.state if case else None
    return Observation(sc.id, sc.playbook_id, sc.family, seed, state, escalated, e.escalation.expected, done_ok,
                       state == e.case_state and done_ok, executed, forb, wrong, claims, unsupported,
                       case.touches if case else 0, turns, verify_chain(world.conn).ok, rejected, max_touches=e.max_touches)
