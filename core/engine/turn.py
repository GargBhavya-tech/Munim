"""Production assembly: the turn agent the engine calls for every decision (Bible sections 13, 27-29, 35).

``TurnAgent`` is the engine's ``decider``. It wires the pieces that are each built and tested on their own, in this order:

  1. which message     only the FOCUS case of a merchant message answers it (the case of the message's intent, else the most urgent
                       open case), and only once per message; the other cases it wakes only re-diagnose (no model call, they wait);
                       system events (a settlement failed, KYC submitted, a timer) are system turns with no merchant text
  2. read-back         a pending low-confidence confirmation is resolved first: a clear yes runs the held action, nothing else does (#40)
  3. language          explicit switch / a pending "shall we speak X?" / what speech recognition heard (#41); code picks the reply language
  4. sources           this turn's SourceBook: the playbook's required reads (tool:...), KB passages in the playbook's scope (kb:...),
                       the policy rules (policy:...). The model may only cite these ids (#43)
  5. context           the bounded, fenced prompt (#37)
  6. decide            the claim-only TurnDecision, one retry on invalid output (#36); an outage or a second invalid output escalates
  7. guard             the model's proposal checked against the allowlist, args, provenance and a policy dry run (#38)
  8. tier              the soft score from observable signals, never the model's confidence; BLUE/RED carry no action (#50)
  9. read-back hold    a write on low-confidence speech is held and read back by code (#40)
 10. grounding gate    verify -> render -> faithfulness -> compliance; one rewrite of the decision on REWRITE (#43-#49)

What it hands back is the engine's ``Decision``: the action the guard let through, the gate's text as the message (already in the
merchant's language), and ``escalate``/``park`` when a human or fix-first must take over. The engine still applies its own allowlist,
fix-first, playbook limits and contact budget, and the executor still re-evaluates policy at execution time: nothing here replaces them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from core.engine.case_manager import RANK
from core.engine.cases import TERMINAL
from core.engine import merchant_undo
from core.engine.escalation import INVALID_OUTPUT_TWICE, TOOL_FAILURE, SoftScorer
from core.engine.intents import CONTROL_INTENTS, INTENT_TO_PLAYBOOK, top_intent
from core.engine.dispatcher import GREETING_PLAYBOOK
from core.engine.triage import triage_message
from core.engine.workflow import MERCHANT_INITIATED, Decision, DecisionContext
from core.gate.pipeline import CLARIFY, ESCALATE, NO_REPLY, REWRITE, SEND, GateOutcome, GroundingGate
from core.gate.sources import Claim, SourceBook
from core.gate.verify import _fmt_value, _label
from core.gate.tiers import TierResult, decide_tier, enforce, prior_blue_turns
from core.llm.client import LLMUnavailable
from core.llm.decision import TurnDecision, decide
from core.llm.sarvam_http import SarvamError
from core.memory.context import ContextBuilder, ContextOverflow, Turn, TurnInputs
from core.memory.redact import redact_or_drop
from core.voice.language import normalise_code
from core.voice.readback import PROMPT as READBACK_PROMPT

ANSWERED_KEY = "turn_answered:"          # meta key prefix: the merchant message (root event id) -> the case that answered it
MAX_LIST_ITEMS = 3                        # first list items of a read shown to the model (reads list newest first; the SourceBook keeps all)
MAX_KB_PASSAGES = 3
DEFAULT_LANGUAGE = "hi-IN"
PARK_NOTE = "I'll come back to this once your other issue is sorted."


@dataclass(frozen=True)
class Topic:
    """What the merchant actually asked (triaged intent) when this case belongs to another playbook."""
    intent: str
    playbook_id: str
    reads: tuple[str, ...]          # the reads that can answer it (empty: nothing in the system does)
    case_reads: tuple[str, ...]     # this case's own reads, which must not be passed off as the answer


@dataclass
class TurnRecord:
    """What one call did; kept on the agent (``last``) for tests and the dashboard's debugging, never used for decisions."""
    case_id: str
    merchant_message: str = ""
    language: str = DEFAULT_LANGUAGE
    path: str = ""                       # READBACK_CONFIRMED | READBACK_REASK | LANGUAGE_CONFIRM | ESCALATE_UPSTREAM | ... | GATE_<status>
    decision: TurnDecision | None = None
    gate: GateOutcome | None = None
    tier: str = "WHITE"
    sources: list[str] = field(default_factory=list)
    escalate: list[str] = field(default_factory=list)


def root_event_id(event_id: str) -> str:
    """The dispatcher fans one event out to every open case as ``<event_id>@<case_id>``; they are the same merchant message."""
    return event_id.split("@", 1)[0]


def message_focus(open_cases: Sequence[Any], intent: str | None) -> str | None:
    """The one case that answers a merchant message: the open case of the message's playbook, else the most urgent open case."""
    live = [c for c in open_cases if c.state not in TERMINAL]
    if not live:
        return None
    pid = INTENT_TO_PLAYBOOK.get(intent or "")
    mine = [c for c in live if c.playbook_id == pid]
    pool = mine or live
    return sorted(pool, key=lambda c: (RANK.get(c.lane, 9), c.opened_at, c.case_id))[0].case_id


def _trim(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _trim(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_trim(x) for x in v[:MAX_LIST_ITEMS]]
    return v


class TurnAgent:
    def __init__(self, conn: sqlite3.Connection, clock, ledger, registry, reader: Callable[[str, dict], dict], *, client, guard, gate: GroundingGate,
                 context_builder: ContextBuilder | None = None, knowledge=None, readback=None, language=None, scorer: SoftScorer | None = None,
                 profile=None, policy_rules: Sequence[Any] = (), style_fn: Callable[[str], str | None] | None = None, executor=None,
                 handlers: Mapping[str, Callable[[DecisionContext, str], Decision]] | None = None):
        self.handlers = dict(handlers or {})            # playbook id -> code decider (Scam Shield): no model on that playbook
        self.executor = executor                         # merchant undo runs the compensation through it (#65)
        self.conn, self.clock, self.ledger, self.registry, self.reader = conn, clock, ledger, registry, reader
        self.client, self.guard, self.gate = client, guard, gate
        self.context = context_builder or ContextBuilder()
        self.knowledge, self.readback, self.language, self.profile = knowledge, readback, language, profile
        self.scorer = scorer or SoftScorer()
        self.policy_rules = tuple(policy_rules)
        self.style_fn = style_fn
        self.engine = None
        self.last: TurnRecord | None = None

    def bind(self, engine) -> "TurnAgent":
        """The engine opens and parks the cases a merchant raises in passing (``new_case_requests``)."""
        self.engine = engine
        return self

    # ================================================================================================================ the turn
    def __call__(self, ctx: DecisionContext) -> Decision:
        case, ev = ctx.case, ctx.event
        mid = case.subject_id
        msg, payload = self._merchant_message(ctx)
        rec, prev = TurnRecord(case.case_id, msg), self.last
        self.last = rec
        try:
            return self._turn(ctx, rec, msg, payload, mid)
        finally:
            if rec.path == "WOKEN_ONLY":
                self.last = prev                             # keep the record of the turn that answered
            if msg:
                self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (ANSWERED_KEY + root_event_id(ev.event_id), case.case_id))

    def _turn(self, ctx: DecisionContext, rec: TurnRecord, msg: str, payload: dict, mid: str) -> Decision:
        case, ev = ctx.case, ctx.event
        if not msg and ev.type in MERCHANT_INITIATED:
            rec.path = "WOKEN_ONLY"                      # another case answers this message; this one only re-diagnosed: wait
            return Decision()
        lang = self._language(ctx, rec, msg, payload, mid)
        if isinstance(lang, Decision):
            return lang
        rec.language = lang

        if msg and self.readback is not None and self.readback.pending(case.case_id, mid):
            res = self.readback.resolve(case.case_id, mid, msg)
            if res.status == "CONFIRMED":
                rec.path = "READBACK_CONFIRMED"
                return Decision(action=(res.action, dict(res.args)), language=lang)
            if res.status == "UNCLEAR":
                rec.path = "READBACK_REASK"
                slot = self.conn.execute("SELECT slot_text FROM voice_confirmations WHERE confirmation_id = ?", (res.confirmation_id,)).fetchone()[0]
                return Decision(message_en=READBACK_PROMPT.get(lang, READBACK_PROMPT["en-IN"]).format(slot=slot), language=lang)
            # DECLINED: the held action is gone; the merchant's words are an ordinary turn now

        if case.playbook_id in self.handlers:            # a playbook decided by code: the model is never asked
            rec.path = "CODE_HANDLER"
            return self.handlers[case.playbook_id](ctx, lang)

        if msg and self.executor is not None and (payload.get("intent") or top_intent(msg)) == "UNDO_LAST":
            out, undone = merchant_undo.undo_last(self.executor, self.conn, self.clock, case.case_id)    # THIS case only
            ok = out is not None and out.status in ("OK", "REPLAYED")
            rec.path = "UNDO_DONE" if ok else "UNDO_NOTHING"
            return Decision(message_en=merchant_undo.reply(lang, undone, ok), language=lang)

        topic = self._topic(ctx, msg, payload)
        book, case_lines, outcomes = self._sources(ctx, mid, topic)
        asked = (payload.get("intent") or top_intent(msg)) if msg else None
        if asked and topic is None:                      # the model is told, by code, what the merchant asked (their words are fenced separately)
            case_lines.insert(1, f"[intent] Merchant's message triaged as {asked}: your reply must address it.")
        passages, retrieval_score = self._knowledge(ctx, msg, book)
        rec.sources = sorted([*book.tool_results, *book.kb_chunks, *book.policy_rules])
        if outcomes:                                     # a system read failed: nothing can be proven, a human takes it
            return self._handoff(rec, lang, case.case_id, [f"{TOOL_FAILURE}: {outcomes[0]}"], msg, "ESCALATE_READ_FAILED")

        try:
            built = self.context.build(self._inputs(ctx, msg, case_lines, passages, mid))
        except ContextOverflow as e:
            return self._handoff(rec, lang, case.case_id, [f"CONTEXT_OVERFLOW: {e}"], msg, "ESCALATE_CONTEXT")

        first_contact = self._first_contact(mid)
        rewrite_used, messages = False, built.messages
        for _ in (1, 2):
            if case.playbook_id == GREETING_PLAYBOOK:    # a bare greeting needs no model: the decision is "ask what they need", fixed here in code
                td = rec.decision = TurnDecision(language=lang, ask_clarification=True)
            else:
                dr = decide(self.client, messages, case_id=case.case_id, ledger=self.ledger)
                if dr.outcome_error:
                    return self._handoff(rec, lang, case.case_id, [f"{TOOL_FAILURE}: {dr.outcome_error}"], msg, "ESCALATE_UPSTREAM")
                if dr.decision is None:
                    return self._handoff(rec, lang, case.case_id, [f"{INVALID_OUTPUT_TWICE}: {'; '.join(dr.errors)}"], msg, "ESCALATE_INVALID")
                td = rec.decision = dr.decision
            gr = self.guard.check(ctx, td, msg, book.tool_results)
            self._new_cases(ctx, gr.new_cases, lang)
            if gr.escalate_reasons:
                return self._handoff(rec, lang, case.case_id, gr.escalate_reasons, msg, "ESCALATE_GUARD")

            tier = self._tier(ctx, td, msg, payload, retrieval_score, passages)
            rec.tier = tier.tier
            decision = enforce(tier, gr.decision)
            action = decision.action

            if action and self.readback is not None and payload.get("low_confidence"):
                hold = self.readback.review(case.case_id, mid, action[0], action[1], low_confidence=True, language=lang)
                if hold.outcome == "HOLD":
                    rec.path = "READBACK_HELD"
                    return Decision(message_en=hold.prompt, language=lang)

            claims = [Claim(c.id, c.text, c.source) for c in td.claims_en] if tier.tier == "WHITE" else []
            if topic is not None and tier.tier == "WHITE":
                claims = self._on_topic(claims, topic)
                if not claims:                           # nothing this case knows answers what was asked: say so and hand over, never answer another question
                    return self._handoff(rec, lang, case.case_id, [f"TOPIC_MISMATCH: {topic.intent} is not answered by {case.playbook_id}"], msg,
                                         "ESCALATE_TOPIC")
            try:
                out = self.gate.run(claims=claims, book=book, language=lang, case_id=case.case_id, first_contact=first_contact and bool(msg),
                                    merchant_message=msg, tier=tier, style_hint=self.style_fn(mid) if self.style_fn else None, rewrite_used=rewrite_used)
            except (LLMUnavailable, SarvamError, OSError) as e:     # the renderer or the back-translation is down: say nothing unproven
                return self._handoff(rec, lang, case.case_id, [f"{TOOL_FAILURE}: UPSTREAM_UNAVAILABLE: {type(e).__name__}"], msg, "ESCALATE_RENDER")
            rec.gate, rec.path = out, f"GATE_{out.status}"
            if out.status == REWRITE:
                rewrite_used = True
                messages = list(built.messages) + [{"role": "user", "content": "GROUNDING_FEEDBACK: " + (out.rewrite_feedback or "")}]
                continue
            park = PARK_NOTE if gr.park else None
            if out.status == ESCALATE:
                rec.escalate = list(out.reasons)
                return Decision(message_en=out.text, language=lang, escalate=list(out.reasons))
            if out.status == CLARIFY:
                return Decision(message_en=out.text, language=lang)
            if out.status == NO_REPLY:
                return Decision(action=action, language=lang, follow_up_hours=decision.follow_up_hours, park=park)
            assert out.status == SEND
            return Decision(action=action, message_en=out.text, language=lang, park=park)
        return self._handoff(rec, lang, case.case_id, ["CLAIMS_UNVERIFIED"], msg, "ESCALATE_REWRITE")   # unreachable: the gate escalates

    # ================================================================================================================ resolution
    STATUS_KEYS = ("status", "last_heartbeat")        # what a "we checked" line may state, read straight from the system

    def resolution_notice(self, case, ev, open_cases) -> Decision | None:
        """The engine calls this when the done-check passed on the merchant's own message and nothing has been said in the case yet
        (the live finding: "soundbox not working" while the device reads ONLINE closed the case in silence). The claims are built by CODE
        from the playbook's reads (the exact sentence the verbaliser gives the source, so the numbers cannot differ) and go through the
        same gate as every other reply. Only the case that answers this message speaks, and only once."""
        payload = dict(ev.payload or {})
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        if self.conn.execute("SELECT 1 FROM meta WHERE key = ?", (ANSWERED_KEY + root_event_id(ev.event_id),)).fetchone():
            return None
        intent = payload.get("intent") or top_intent(text)
        if message_focus(open_cases, intent) not in (None, case.case_id):
            return None
        mid, pb = case.subject_id, self.engine.playbooks[case.playbook_id]
        book, claims = SourceBook(), []
        for n, read in enumerate(pb.required_reads, 1):
            try:
                body = self.reader(read, {"merchant_id": mid})
            except Exception as e:
                rec = TurnRecord(case.case_id, text)
                self.last = rec
                return self._handoff(rec, normalise_code(case.language) or DEFAULT_LANGUAGE, case.case_id, [f"{TOOL_FAILURE}: {read}: {type(e).__name__}"],
                                     text, "ESCALATE_READ_FAILED")
            sid = f"tool:{read}#r{n}"
            book.add_tool_result(sid, body)
            if isinstance(body, dict):
                subject = _label(read.removeprefix("get_").removeprefix("check_").removesuffix("_status"))
                for key in self.STATUS_KEYS:
                    if body.get(key) is not None:
                        claims.append(Claim(f"r{len(claims) + 1}", f"{subject.capitalize()} {_label(key)} is {_fmt_value(key, body[key])}.", sid))
        if not claims:
            return None
        lang = self.language.current(mid, case.language) if self.language is not None else (normalise_code(case.language) or DEFAULT_LANGUAGE)
        rec = TurnRecord(case.case_id, text, lang, "RESOLUTION_NOTICE")
        self.last = rec
        try:
            out = self.gate.run(claims=claims, book=book, language=lang, case_id=case.case_id, first_contact=self._first_contact(mid),
                                merchant_message=text, rewrite_used=True)
        except (LLMUnavailable, SarvamError, OSError) as e:
            return self._handoff(rec, lang, case.case_id, [f"{TOOL_FAILURE}: UPSTREAM_UNAVAILABLE: {type(e).__name__}"], text, "ESCALATE_RENDER")
        rec.gate = out
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (ANSWERED_KEY + root_event_id(ev.event_id), case.case_id))
        if out.status == SEND:
            return Decision(message_en=out.text, language=lang)
        if out.status == ESCALATE:
            rec.escalate = list(out.reasons)
            return Decision(message_en=out.text, language=lang, escalate=list(out.reasons))
        return None

    # ================================================================================================================ steps
    def _merchant_message(self, ctx: DecisionContext) -> tuple[str, dict]:
        """(redacted merchant text, event payload) when THIS case answers this merchant message; ("", payload) otherwise."""
        ev, payload = ctx.event, dict(ctx.event.payload or {})
        if ev.type not in MERCHANT_INITIATED or not str(payload.get("text") or "").strip():
            return "", payload
        if self.conn.execute("SELECT 1 FROM meta WHERE key = ?", (ANSWERED_KEY + root_event_id(ev.event_id),)).fetchone():
            return "", payload                               # already answered (here, earlier in this event, or by another case)
        intent = payload.get("intent") or top_intent(str(payload["text"]))
        if message_focus(ctx.open_cases, intent) not in (None, ctx.case.case_id):
            return "", payload
        red = redact_or_drop(str(payload["text"]))           # the voice path redacted already; a text channel may not have
        return (red.text if red is not None else ""), payload

    def _language(self, ctx: DecisionContext, rec: TurnRecord, msg: str, payload: dict, mid: str) -> str | Decision:
        case, ev = ctx.case, ctx.event
        if self.language is None:
            return normalise_code(case.language) or DEFAULT_LANGUAGE
        lang = self.language.current(mid, case.language)
        if not msg:
            return lang
        flip = self.language.answer_flip(mid, msg, case_id=case.case_id, event_id=ev.event_id)
        if flip.changed:
            return flip.language
        sw = self.language.apply_explicit_switch(mid, msg, case_id=case.case_id, event_id=ev.event_id, current=lang)
        if sw.changed:
            return sw.language
        if payload.get("channel") == "voice" and payload.get("language"):
            obs = self.language.observe_stt(mid, payload.get("language"), payload.get("stt_confidence"), case_language=case.language)
            if obs.action == "CONFIRM":
                rec.path = "LANGUAGE_CONFIRM"
                return Decision(message_en=obs.prompt, language=obs.reply_language)
            return obs.reply_language
        return lang

    def _topic(self, ctx: DecisionContext, msg: str, payload: dict) -> "Topic | None":
        """The merchant's actual question when this case's playbook is NOT about it (their case is open on something else). None when
        the case is the topic's own, the intent is unknown or a control intent, or this is a system turn."""
        if not msg:
            return None
        intent = payload.get("intent") or top_intent(msg)
        if not intent or intent in CONTROL_INTENTS:
            return None
        pid = INTENT_TO_PLAYBOOK.get(intent)
        if pid is None or pid == ctx.case.playbook_id:
            return None
        pb = (getattr(self.engine, "playbooks", None) or {}).get(pid)
        reads = tuple(pb.required_reads) if pb is not None and pid != "X0_UNSUPPORTED" else ()
        return Topic(intent, pid, reads, tuple(ctx.playbook.required_reads))

    @staticmethod
    def _on_topic(claims: list[Claim], topic: "Topic") -> list[Claim]:
        """Drop claims whose source is a read of the case's own playbook that the merchant's topic does not also read."""
        foreign = set(topic.case_reads) - set(topic.reads)
        def read_of(c: Claim) -> str | None:
            return c.source.split(":", 1)[1].split("#", 1)[0] if c.source.startswith("tool:") else None
        return [c for c in claims if read_of(c) not in foreign]

    def _sources(self, ctx: DecisionContext, mid: str, topic: "Topic | None" = None) -> tuple[SourceBook, list[str], list[str]]:
        book, lines, failures = SourceBook(), [], []
        lines.append(f"[case] {ctx.case.case_id} playbook {ctx.case.playbook_id} step {ctx.step.name} touches {ctx.case.touches}")
        reads = list(ctx.playbook.required_reads)
        if topic is not None:                        # the topic's reads come FIRST: the case-facts budget is small and the answer must not be cut
            lines.append(f"[topic] Merchant asked {topic.intent} ({topic.playbook_id}), not {ctx.case.playbook_id}: answer only that; claim nothing from other facts. "
                         + ("" if topic.reads else "No system read answers it: say you cannot answer it here and will pass it on."))
            reads = list(topic.reads) + [r for r in reads if r not in topic.reads]
        for n, read in enumerate(reads, 1):
            try:
                body = self.reader(read, {"merchant_id": mid})
            except Exception as e:                             # ReadError, circuit open, upstream down: never guessed around
                failures.append(f"{read}: {getattr(getattr(e, 'args', [None])[0], 'error', None) or type(e).__name__}")
                continue
            sid = f"tool:{read}#r{n}"
            book.add_tool_result(sid, body)
            lines.append(f"[{sid}] {json.dumps(_trim(body), ensure_ascii=False, sort_keys=True, default=str)}")
        for r in self.policy_rules:
            book.add_policy(f"policy:{r.id}", r.title)
        return book, lines, failures

    def _knowledge(self, ctx: DecisionContext, msg: str, book: SourceBook) -> tuple[list[tuple[str, str]], float | None]:
        scope = list(ctx.playbook.kb_scope)
        if self.knowledge is None or not scope:
            return [], None
        kb, _hints = self.knowledge.search(msg or ctx.step.name.replace("_", " "), scope)
        out = []
        for p in kb[:MAX_KB_PASSAGES]:
            sid = f"kb:{p.chunk_id}"
            book.add_kb(sid, p.text)
            out.append((sid, p.text))
        return out, (kb[0].score if kb else None)

    def _inputs(self, ctx: DecisionContext, msg: str, case_lines: list[str], passages: list[tuple[str, str]], mid: str) -> TurnInputs:
        pb, step = ctx.playbook, ctx.step
        actions = []
        for name in ctx.allowlist:
            if self.registry.has(name):
                fields = ", ".join(self.registry.get(name).args_model.model_fields)
                actions.append(f"- {name}({fields})")
        playbook_lines = [f"PLAYBOOK {pb.id} ({pb.half}, lane {pb.priority_lane}). Current step: {step.name}. The step is done when: {step.done}.",
                          "ALLOWED ACTIONS (use these exact names; merchant_id and case_id are filled by the system):", *(actions or ["- none"])]
        timeline = [f"{c.case_id} {c.playbook_id} lane {c.lane} state {c.state} step {c.step}" for c in ctx.open_cases if c.case_id != ctx.case.case_id]
        sys_facts, said = [], []
        if self.profile is not None:
            for k, f in sorted(self.profile.current(mid).items()):
                (sys_facts if f.source == "SYSTEM" else said).append(f"{k}: {f.value}")
        knowledge = list(passages) + [(sid, str(t)) for sid, t in ((f"policy:{r.id}", r.title) for r in self.policy_rules)]
        return TurnInputs(playbook_lines=playbook_lines, case_facts=case_lines, timeline=timeline, profile_system=sys_facts,
                          profile_merchant_said=said, knowledge=knowledge, turns=self._turns(ctx), merchant_message=msg)

    def _turns(self, ctx: DecisionContext) -> list[Turn]:
        """Earlier conversation of this case, oldest first: merchant messages (already redacted at the gateway) and what Munim sent."""
        rows = []
        for r in self.conn.execute("SELECT e.ts_sim, e.seq, e.event_id, e.payload_json FROM case_events ce JOIN events e ON e.event_id = ce.event_id "
                                   "WHERE ce.case_id = ? AND e.type IN ('MERCHANT_MESSAGE', 'LEAD_REPLY')", (ctx.case.case_id,)):
            if root_event_id(r["event_id"]) == root_event_id(ctx.event.event_id):
                continue
            text = json.loads(r["payload_json"]).get("text")
            if text:
                red = redact_or_drop(str(text))
                rows.append((r["ts_sim"], 0, r["seq"], "IN", red.text if red else "[unreadable]"))
        for e in self.ledger.entries(ctx.case.case_id):
            if e.kind == "MESSAGE_SENT" and e.payload.get("text"):
                rows.append((e.ts_sim, 1, e.seq, "OUT", e.payload["text"]))
        return [Turn(d, t) for *_, d, t in sorted(rows)]

    def _first_contact(self, mid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM ledger l JOIN cases c ON c.case_id = l.case_id WHERE l.kind = 'MESSAGE_SENT' "
                                 "AND (c.merchant_id = ? OR c.lead_id = ?) LIMIT 1", (mid, mid)).fetchone() is None

    def _tier(self, ctx: DecisionContext, td: TurnDecision, msg: str, payload: dict, retrieval_score: float | None,
              passages: list) -> TierResult:
        prior = prior_blue_turns(self.ledger.entries(ctx.case.case_id))
        if not msg:                                         # a system turn: there is no merchant to be unsure about
            return decide_tier(0.0, prior_blue_turns=0, ask_clarification=False)
        if ctx.playbook.id == GREETING_PLAYBOOK:            # a bare greeting asks nothing to be answered: say who we are and ask what they need (the fixed
            return TierResult("BLUE", None, 0.0)            # clarifying line), whatever the model proposed. It is never "outside knowledge" or a hand-over.
        rule = payload.get("intent") or top_intent(msg)
        uncertainty = triage_message(msg, [(i.intent, i.w) for i in td.intents], rule, INTENT_TO_PLAYBOOK).uncertainty if td.intents else 0.5
        pb = ctx.playbook
        outside = bool(pb.kb_scope) and not pb.required_reads and not passages     # a knowledge-only playbook with nothing approved to say
        soft = self.scorer.assess(text=msg, language=payload.get("language"), intent_uncertainty=uncertainty, retrieval_score=retrieval_score,
                                  stt_confidence=payload.get("stt_confidence"), prior_blue_turns=prior, outside_knowledge=outside)
        if soft.tier == "RED":
            return TierResult("RED", soft.reason, soft.score)
        return decide_tier(soft.score, config=self.scorer.config, prior_blue_turns=prior, ask_clarification=td.ask_clarification)

    def _new_cases(self, ctx: DecisionContext, dispositions, lang: str) -> None:
        if self.engine is None:
            return
        for d in dispositions:
            if d.disposition not in ("OPEN", "PARK"):
                continue
            case = self.engine.open_case(ctx.case.merchant_id, d.playbook_id, lead_id=ctx.case.lead_id, language=lang)
            if d.disposition == "PARK":
                self.engine.case_manager.park(case, PARK_NOTE)

    def _handoff(self, rec: TurnRecord, lang: str, case_id: str, reasons: list[str], msg: str, path: str) -> Decision:
        """A fixed hand-off line through the gate (so it is ledgered as a VERIFY entry with compliance lines), then escalate."""
        out = self.gate.run(claims=[], book=SourceBook(), language=lang, case_id=case_id, first_contact=False, merchant_message=msg,
                            tier=TierResult("RED", reasons[0]))
        rec.gate, rec.path, rec.escalate = out, path, list(reasons)
        return Decision(message_en=out.text if msg else None, language=lang, escalate=list(reasons))
