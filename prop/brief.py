"""Morning brief for the soundbox prop (#133).

Every sentence comes out of the grounding gate (verified claims only). The brief may also carry PROPOSALS (an action the merchant could
approve), but a proposal is only ever HELD in the read-back gate: nothing is submitted until the merchant says yes ("haan") to the
read-back. Silence, timeout, a no, or no answer at all never executes anything. The read-back is forced (``low_confidence=True``)
because the brief spoke first: the merchant did not ask for the action.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.gate.pipeline import SEND, GateOutcome, GroundingGate
from core.gate.sources import Claim, SourceBook
from core.voice.readback import Hold, ReadBackGate


@dataclass(frozen=True)
class Proposal:
    action: str
    args: dict


@dataclass
class Brief:
    outcome: GateOutcome
    text: str | None                     # spoken text; None unless every sentence passed the gate
    holds: list[Hold] = field(default_factory=list)   # read-back prompts for proposals, spoken after the brief


class MorningBrief:
    def __init__(self, gate: GroundingGate, readback: ReadBackGate):
        self.gate, self.readback = gate, readback

    def build(self, merchant_id: str, claims: list[Claim], book: SourceBook, *, language: str, proposals: list[Proposal] = (),
              case_id: str | None = None) -> Brief:
        out = self.gate.run(claims=claims, book=book, language=language, case_id=case_id)
        if out.status != SEND or not out.text:
            return Brief(out, None)                      # an unverified brief is not spoken, and proposes nothing
        holds = [self.readback.review(case_id, merchant_id, p.action, p.args, low_confidence=True, language=language) for p in proposals]
        return Brief(out, out.text, [h for h in holds if h.outcome == "HOLD"])
