"""Production assembly (no ticket number; BUILD_STATUS "the biggest gap"): one place that wires a whole talking Munim.

``build_turn_agent`` puts the model-side pieces together (turn agent, decision guard, grounding gate, read-back, language manager) around
an engine's existing collaborators; ``hard_escalation`` combines the playbook's escalate_when with the hard triggers (a request for a person,
regulatory words, fraud flags ...); ``wire`` makes the agent the engine's decider and the handoff service its escalation writer.
The dev server and the tests use these, so there is exactly one wiring. Providers (LLM, NLI, translator, retriever) are injected:
Sarvam and the real entailment model in production, scripted fakes in tests.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from core.engine.case_facts import replacements_before_case
from core.engine.diagnose import escalation_reasons
from core.engine.escalation import HardTriggers, SoftScorer
from core.engine.guard import DecisionGuard
from core.engine.handoff import HandoffService
from core.engine.turn import TurnAgent
from core.gate.compliance import ComplianceChecker
from core.gate.faithfulness import FaithfulnessChecker
from core.gate.phrases import Phrases
from core.gate.pipeline import GroundingGate
from core.gate.render import Renderer
from core.gate.verify import GroundingVerifier
from core.memory.context import ContextBuilder
from core.memory.profile import ProfileStore
from core.voice.language import LanguageManager, normalise_code
from core.voice.readback import ReadBackGate


def build_gate(client, nli, translator, *, ledger=None, phrases: Phrases | None = None, embedder=None,
               aliases: Mapping[str, str] | None = None) -> GroundingGate:
    """``aliases``: the pack's gate_aliases.yaml (claim word -> source word), read by the entailment check only."""
    phrases = phrases or Phrases()
    return GroundingGate(GroundingVerifier(nli, aliases=aliases), Renderer(client, phrases), FaithfulnessChecker(nli, translator, embedder),
                         compliance=ComplianceChecker(phrases), phrases=phrases, ledger=ledger)


def build_turn_agent(engine, *, policy, client, nli, translator, knowledge=None, executor=None, phrases: Phrases | None = None,
                     voice_config: Mapping | None = None, scorer: SoftScorer | None = None, style_fn: Callable[[str], str | None] | None = None,
                     embedder=None, handlers=None, aliases: Mapping[str, str] | None = None) -> TurnAgent:
    """``engine`` is a core.engine.workflow.Engine (its conn, clock, ledger, registry, reader, playbooks and case manager are reused).
    ``executor`` runs the language preference write (save_preference); default: the engine's executor."""
    conn, clock, ledger = engine.conn, engine.clock, engine.ledger
    profile = ProfileStore(conn, clock)
    guard = DecisionGuard(engine.registry, engine.case_manager, engine.reader, policy, engine.playbooks, ledger)
    gate = build_gate(client, nli, translator, ledger=ledger, phrases=phrases, embedder=embedder, aliases=aliases)
    cfg = dict(voice_config) if voice_config is not None else None
    agent = TurnAgent(conn, clock, ledger, engine.registry, engine.reader, client=client, guard=guard, gate=gate, context_builder=ContextBuilder(),
                      knowledge=knowledge, readback=ReadBackGate(conn, clock, engine.registry, config=cfg, ledger=ledger),
                      language=LanguageManager(conn, clock, profile, executor or engine.executor, config=cfg), scorer=scorer, profile=profile,
                      policy_rules=getattr(policy, "rules", ()), style_fn=style_fn, executor=executor or engine.executor,
                      handlers=handlers)
    return agent.bind(engine)


def hard_escalation(playbooks: Mapping[str, Any], reader, clock, triggers: HardTriggers | None = None) -> Callable[[Any, Any], list[str]]:
    """The engine's hard-escalation hook: the playbook's own escalate_when, then every hard trigger (Bible section 32)."""
    triggers = triggers or HardTriggers()

    def hook(case, ev) -> list[str]:
        pb = playbooks[case.playbook_id]
        facts = {"touches": case.touches, "intent": ev.payload.get("intent"), "replacements_last_30d": replacements_before_case(reader, clock, case)}
        reasons = list(escalation_reasons(pb, reader, case.subject_id, facts))
        return reasons + [t.as_reason() for t in triggers.check(case, ev, {"intent": ev.payload.get("intent"), "text": ev.payload.get("text")})]
    return hook


def wire(engine, agent: TurnAgent, *, handoffs: HandoffService | None = None) -> TurnAgent:
    """Make ``agent`` the engine's decider; with a handoff service, every escalation writes a full handoff packet."""
    engine.decider = agent
    engine.resolution_fn = agent.resolution_notice
    phrases = getattr(agent.gate, "phrases", None)
    if phrases is not None and phrases.has("handoff"):        # a reply the policy refuses becomes the fixed hand-over line + a person (found live, C1)
        engine.refused_message_line = lambda language: phrases.text("handoff", normalise_code(language) or "en-IN")
    agent.bind(engine)
    if handoffs is not None:
        engine.on_escalate = lambda case, reasons, violations: handoffs.create_packet(case, reasons, violations, facts={"language": case.language})
    return agent
