"""The grounding gate, end to end (Bible section 29; Build Map #43-#50). Nothing reaches the merchant except through ``GroundingGate.run``.

  tier        BLUE  -> one fixed clarifying question, no claims, no action          (#50)
              RED   -> a fixed hand-off line, the case is escalated
  verify      source exists, numbers match the source, the source entails the claim, level by code     (#43-#46)
              blocked claims are removed and listed with their reason; if NO claim survives: one rewrite, then escalate
  render      the reply is written from the verified claims only                                           (#47)
  faithful    every claim sentence is back-translated and must be entailed by a verified claim            (#48)
              a failure re-renders once with the reasons; a second failure escalates (the claims are kept for the human)
  compliance  identity + PIN/OTP line on first contact, a truthful yes to "are you an AI?", reply in the preferred language   (#49)
Each call writes ONE ``VERIFY`` ledger entry: the claim chips (level, colour, reason, rule id), the sentences and their checks, the status.
Statuses: SEND (text is safe to send), CLARIFY, ESCALATE (text is the fixed hand-off line), REWRITE (ask the turn agent once more, with the
feedback), NO_REPLY (nothing groundable and nothing required to say).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.gate.compliance import ComplianceChecker, is_ai_question, language_ok
from core.gate.faithfulness import FaithfulnessChecker, FaithResult
from core.gate.phrases import Phrases
from core.gate.render import RenderedReply, RenderedSentence, RenderError, Renderer
from core.gate.sources import Claim, SourceBook
from core.gate.tiers import TierResult
from core.gate.verify import ClaimVerdict, GroundingVerifier, verbalise

SEND, CLARIFY, ESCALATE, REWRITE, NO_REPLY = "SEND", "CLARIFY", "ESCALATE", "REWRITE", "NO_REPLY"


@dataclass
class GateOutcome:
    status: str
    text: str | None
    sentences: list[RenderedSentence] = field(default_factory=list)
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    tier: str = "WHITE"
    reasons: list[str] = field(default_factory=list)          # why it escalated (feed engine.escalate)
    rewrite_feedback: str | None = None
    faithfulness_failures: int = 0                            # feed HardTriggers facts["faithfulness_failures"]
    grounding_failures: int = 0                               # feed HardTriggers facts["grounding_failures"]
    faith: FaithResult | None = None
    attempts: int = 0
    ledger_seq: int | None = None

    @property
    def blocked(self) -> list[ClaimVerdict]:
        return [v for v in self.verdicts if not v.sendable]

    @property
    def sent_claim_ids(self) -> set[str]:
        return {c for s in self.sentences if s.kind == "claim" for c in s.claim_ids} if self.status == SEND else set()


class GroundingGate:
    def __init__(self, verifier: GroundingVerifier, renderer: Renderer, faithfulness: FaithfulnessChecker, *, compliance: ComplianceChecker | None = None,
                 phrases: Phrases | None = None, ledger=None):
        self.verifier, self.renderer, self.faithfulness, self.ledger = verifier, renderer, faithfulness, ledger
        self.phrases = phrases or renderer.phrases
        self.compliance = compliance or ComplianceChecker(self.phrases)

    # ---------------------------------------------------------------------------------------------------------------
    def run(self, *, claims: list[Claim], book: SourceBook, language: str, case_id: str | None = None, first_contact: bool = False,
            merchant_message: str = "", tier: TierResult | None = None, style_hint: str | None = None, rewrite_used: bool = False) -> GateOutcome:
        asked_ai = is_ai_question(merchant_message)
        tier = tier or TierResult("WHITE")
        ctx = dict(language=language, case_id=case_id, first_contact=first_contact, asked_ai=asked_ai, tier=tier.tier)
        self._book = book

        if tier.tier == "BLUE":
            return self._control(CLARIFY, "clarify", [], **ctx)
        if tier.tier == "RED":
            return self._control(ESCALATE, "handoff", [], reasons=[tier.reason or "RED"], **ctx)

        verdicts = self.verifier.verify(claims, book) if claims else []
        sendable = [v for v in verdicts if v.sendable]
        if claims and not sendable:
            why = "; ".join(f"{v.claim_id}: {v.reason}" for v in verdicts)
            if not rewrite_used:
                return self._finish(GateOutcome(REWRITE, None, verdicts=verdicts, tier="WHITE", grounding_failures=1,
                                                rewrite_feedback=f"No claim could be verified ({why}). Use only facts in CASE FACTS or KNOWLEDGE and cite their exact source ids."), ctx)
            return self._control(ESCALATE, "handoff", verdicts, reasons=["CLAIMS_UNVERIFIED: " + why], grounding_failures=2, **ctx)
        if not sendable:
            if asked_ai or first_contact:
                return self._control(SEND, None, verdicts, **ctx)           # the compliance lines are the whole reply
            return self._finish(GateOutcome(NO_REPLY, None, verdicts=verdicts, tier="WHITE"), ctx)

        reply, faith, failures, feedback = None, None, 0, None
        for attempt in (1, 2):
            try:
                reply = self.renderer.render(sendable, language, style_hint=style_hint, feedback=feedback, case_id=case_id)
            except RenderError as e:
                reply, failures, feedback = None, failures + 1, f"render error: {e}"
                continue
            faith = self.faithfulness.check(reply, sendable)
            claim_text = " ".join(s.text for s in reply.claim_sentences)
            wrong_language = bool(claim_text) and not language_ok(claim_text, language)
            if faith.ok and not wrong_language:
                sentences = self.compliance.apply(reply.sentences, language, first_contact=first_contact, asked_ai=asked_ai)
                out = GateOutcome(SEND, " ".join(s.text for s in sentences), sentences, verdicts, "WHITE", [], None, failures, 0, faith, attempt)
                return self._finish(out, ctx)
            failures += 1
            feedback = "; ".join([f"sentence {s.index + 1}: {', '.join(s.reasons)}" for s in faith.failed] + (["the reply must be written in " + language] if wrong_language else []))
        reasons = ["FAITHFULNESS_FAILED_TWICE: " + (feedback or "renderer produced no valid reply")]
        return self._control(ESCALATE, "handoff", verdicts, reasons=reasons, faithfulness_failures=failures, faith=faith, attempts=2, **ctx)

    # ---------------------------------------------------------------------------------------------------------------
    def _control(self, status: str, phrase: str | None, verdicts: list[ClaimVerdict], *, language: str, case_id, first_contact: bool, asked_ai: bool, tier: str,
                 reasons: list[str] | None = None, faithfulness_failures: int = 0, grounding_failures: int = 0, faith: FaithResult | None = None,
                 attempts: int = 0) -> GateOutcome:
        body = [RenderedSentence("control", self.phrases.text(phrase, language), (), phrase)] if phrase else []
        sentences = self.compliance.apply(body, language, first_contact=first_contact, asked_ai=asked_ai)
        out = GateOutcome(status, " ".join(s.text for s in sentences) or None, sentences, verdicts, tier, reasons or [], None, faithfulness_failures,
                          grounding_failures, faith, attempts)
        return self._finish(out, dict(language=language, case_id=case_id, tier=tier, first_contact=first_contact, asked_ai=asked_ai))

    def _chip(self, v: ClaimVerdict, out: GateOutcome) -> dict:
        """The dashboard chip (#63): the verdict plus its evidence. ``sent`` is False for a verified claim that did not reach the merchant
        (removed by faithfulness, or the turn escalated); the case view shows those amber."""
        book = getattr(self, "_book", None)
        exists, body = book.get(v.source) if book is not None else (False, None)
        score = v.nli_probs.get(v.nli_label) if v.nli_label else None
        return {**v.as_chip(), "sent": v.claim_id in out.sent_claim_ids, "nli_label": v.nli_label, "nli_score": score,
                "corroborated_by": list(v.corroborated_by), "snippet": verbalise(v.source, body)[:400] if exists else None}

    def _finish(self, out: GateOutcome, ctx: dict) -> GateOutcome:
        if self.ledger is not None:
            checks = {c.text: c for c in (out.faith.sentences if out.faith else [])}      # by text: compliance lines are prepended to the reply
            out.ledger_seq = self.ledger.append(kind="VERIFY", actor="SYSTEM", case_id=ctx["case_id"], payload={
                "status": out.status, "tier": out.tier, "language": ctx["language"], "attempts": out.attempts, "reasons": out.reasons,
                "claims": [self._chip(v, out) for v in out.verdicts],
                "sentences": [{"kind": s.kind, "text": s.text, "claim_ids": list(s.claim_ids),
                               "ok": checks[s.text].ok if s.text in checks else True, "reasons": list(checks[s.text].reasons) if s.text in checks else [],
                               "back_translation": checks[s.text].back_translation if s.text in checks else None}
                              for s in out.sentences],
                "faithfulness_failures": out.faithfulness_failures, "grounding_failures": out.grounding_failures}).seq
        return out
