"""How to approach a lead: the more valuable the lead and the less we know about them, the more human the first contact.

``jobs/scoring.channel_for`` already maps a lead's value to a channel (a rep visit at high value, a voice call or WhatsApp in the middle, a self-serve link at the bottom).
That ignores how much we actually know. A lead whose value rests on a single signal is a guess, and a guess pitched by a link wastes the lead; so an UNCERTAIN lead is moved one
step toward a person. Value sets the level; ignorance can only raise it, never lower it, and nothing goes above the top.

Two kinds of lead have no rep visit: an online brand (D2C) is not a shop to walk into, so its top level is a human call; a bank or institution is never approached by a
message at all, a human account manager owns it and the AI only does the homework (the brief).

Pure code over the lead's own facts: no model, no clock. The thresholds live in ``jobs/scoring.channel_for``.
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs.outreach import segment_for
from jobs.scoring import channel_for

LEVELS = ("self_serve", "ai_voice", "field_rep")
INSTITUTIONS = frozenset({"bank", "nbfc", "institution"})
HUMAN = "human_account_manager"
D2C_TOP = "human_call"
LEVEL_WORDS = {"self_serve": "A self-serve link", "ai_voice": "A voice call or WhatsApp", "field_rep": "A rep visit, planned on a route",
               D2C_TOP: "A human call", HUMAN: "A human account manager"}


@dataclass(frozen=True)
class Approach:
    level: str
    base_level: str                 # what the value alone would choose
    bumped: bool                    # raised one step because we know little
    reasons: tuple[str, ...]


def approach_for(value: int, *, uncertain: bool, category: str | None) -> Approach:
    if category in INSTITUTIONS:
        return Approach(HUMAN, HUMAN, False, ("A bank or institution is never approached by a message: a human owns it and the AI prepares the brief",))
    base = channel_for(int(value))
    level, reasons = base, [f"Value alone says: {LEVEL_WORDS[base].lower()}"]
    bumped = False
    if uncertain and base != LEVELS[-1]:
        level = LEVELS[LEVELS.index(base) + 1]
        bumped = True
        reasons.append("We know little about this lead (its value rests on a single signal), so the first contact is moved one step toward a person")
    elif uncertain:
        reasons.append("We know little about this lead, and it is already at the most human level")
    if segment_for(category) == "d2c" and level == "field_rep":
        level = D2C_TOP
        reasons.append("An online brand has no shop to visit: its most human contact is a call")
    return Approach(level, base if base != "field_rep" or segment_for(category) != "d2c" else D2C_TOP, bumped, tuple(reasons))
