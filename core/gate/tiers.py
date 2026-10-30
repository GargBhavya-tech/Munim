"""Confidence tiers (#50): WHITE sends, BLUE asks one clarifying question and does nothing, RED does not answer and escalates.

The tier comes from code: the soft score S (never the model's stated confidence), the model's request to clarify, and whether a claim could
not be verified or the question is outside approved knowledge. ``escalation.tier_for`` owns the thresholds (BLUE 0.45 <= S < 0.60, RED at 0.60,
a second BLUE turn in a row becomes RED). What this module adds is ENFORCEMENT: a BLUE or RED turn cannot carry an action.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.engine.escalation import BLUE, BLUE_REPEATED, RED, WHITE, ScoreConfig, tier_for
from core.engine.workflow import Decision as EngineDecision


@dataclass(frozen=True)
class TierResult:
    tier: str                      # WHITE | BLUE | RED
    reason: str | None = None      # why RED
    score: float = 0.0

    @property
    def escalate(self) -> bool:
        return self.tier == RED

    @property
    def asks_question(self) -> bool:
        return self.tier == BLUE


def decide_tier(score: float, *, config: ScoreConfig | None = None, prior_blue_turns: int = 0, ask_clarification: bool = False,
                claim_unverifiable: bool = False, outside_knowledge: bool = False) -> TierResult:
    cfg = config or ScoreConfig()
    tier, reason = tier_for(score, cfg, claim_unverifiable=claim_unverifiable, outside_knowledge=outside_knowledge, prior_blue_turns=prior_blue_turns)
    if tier == WHITE and ask_clarification:                  # the model found it ambiguous: same treatment as a BLUE score
        tier, reason = (RED, BLUE_REPEATED) if prior_blue_turns >= 1 else (BLUE, None)
    return TierResult(tier, reason, score)


def enforce(result: TierResult, decision: EngineDecision) -> EngineDecision:
    """BLUE and RED carry no action and no message from the model; WHITE passes through untouched."""
    if result.tier == WHITE:
        return decision
    return EngineDecision(action=None, message_en=None, follow_up_hours=None, channel=decision.channel)


def prior_blue_turns(ledger_entries) -> int:
    """How many BLUE turns in a row end the case's VERIFY history (a WHITE turn resets the count)."""
    n = 0
    for e in reversed([e for e in ledger_entries if e.kind == "VERIFY" and "tier" in e.payload]):
        if e.payload["tier"] != BLUE:
            break
        n += 1
    return n
