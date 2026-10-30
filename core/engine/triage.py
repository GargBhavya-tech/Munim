"""Triage (Bible §13): what is this event / message about, and which case does it belong to.

Both functions are PURE: no database, no clock. The case engine owns case creation; triage only says
"playbook X" and, if the merchant already has an open case of that playbook, "link to case Y".
"""
from __future__ import annotations

from dataclasses import dataclass

from core.engine.playbook import Playbook

TERMINAL_STATES = frozenset({"RESOLVED", "DECLINED"})
UNSUPPORTED_PLAYBOOK = "X0_UNSUPPORTED"
PRESENT_WEIGHT = 0.5      # an intent at or above this weight is "clearly in the message" (a request of its own)
RIVAL_GAP = 0.2           # rival readings closer than this, across different playbooks, need a question
_LANE_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


@dataclass(frozen=True)
class OpenCase:
    case_id: str
    merchant_id: str
    playbook_id: str
    state: str


@dataclass(frozen=True)
class EventTriage:
    playbook_id: str | None
    link_case_id: str | None


@dataclass(frozen=True)
class IntentRequest:
    intent: str
    playbook_id: str
    weight: float


@dataclass(frozen=True)
class TriageResult:
    requests: list[IntentRequest]
    clarify: bool
    uncertainty: float
    clarify_between: tuple[str, str] | None = None


# ------------------------------------------------------------------------------------------------ events
def _open(cases: list[OpenCase]) -> list[OpenCase]:
    return [c for c in cases if c.state not in TERMINAL_STATES]


def triage_event(event: dict, playbooks: dict[str, Playbook], open_cases: list[OpenCase]) -> EventTriage:
    """Map a system event to a playbook through ``triggers.events``; link to the merchant's open case of that
    playbook if there is one (a duplicate-ish event never opens a second case).

    An event that already carries a ``case_id`` of an open case (TIMER_FIRED, HUMAN_RETURNED) goes to that case.
    The caller creates the case when ``link_case_id`` is None and ``playbook_id`` is not None.
    """
    merchant = event.get("merchant_id")
    live = _open(open_cases)
    cid = event.get("case_id")
    if cid:
        for c in live:
            if c.case_id == cid:
                return EventTriage(c.playbook_id, c.case_id)
    etype = event.get("type") or event.get("event_type")
    hits = [p for p in playbooks.values() if etype in p.triggers.events]
    if not hits:
        return EventTriage(None, None)
    # deterministic when several playbooks claim the same event: most urgent lane, then id
    pb = sorted(hits, key=lambda p: (_LANE_ORDER[p.priority_lane], p.id))[0]
    mine = sorted((c for c in live if c.merchant_id == merchant and c.playbook_id == pb.id), key=lambda c: c.case_id)
    return EventTriage(pb.id, mine[0].case_id if mine else None)


# ---------------------------------------------------------------------------------------------- messages
def triage_message(
    merchant_text: str,
    model_intents: list[tuple[str, float]],
    rule_intent: str | None,
    intent_to_playbook: dict[str, str],
) -> TriageResult:
    """Decide the requests in a merchant message.

    * Each intent maps to a playbook (unmapped -> X0_UNSUPPORTED); intents of the same playbook are one request.
    * Intents with weight >= 0.5 are both clearly present: different playbooks -> TWO requests, not rivals.
    * ``w1`` is the top weight; ``w2`` the best weight among intents competing for the same decision, i.e. a
      different playbook that is NOT itself clearly present. A rival within 0.2 of the top -> clarify (BLUE), no action.
    * uncertainty = min(1, (1 - (w1 - w2)) + 0.3 * [rule intent != model top intent]).
    """
    del merchant_text  # reserved for the clarifying-question renderer; the decision uses intents only
    best: dict[str, tuple[str, float]] = {}
    for intent, w in sorted(model_intents, key=lambda t: -t[1]):
        pid = intent_to_playbook.get(intent, UNSUPPORTED_PLAYBOOK)
        if pid not in best and w > 0:
            best[pid] = (intent, float(w))
    if not best:
        if rule_intent:
            # only the keyword detector fired: one request, with a middling weight (it is not a calibrated score)
            pid = intent_to_playbook.get(rule_intent, UNSUPPORTED_PLAYBOOK)
            return TriageResult([IntentRequest(rule_intent, pid, 0.5)], False, 0.5)
        return TriageResult([], True, 1.0)
    ranked = [(pid, i, w) for pid, (i, w) in sorted(best.items(), key=lambda kv: -kv[1][1])]
    top_pid, top_intent, w1 = ranked[0]
    present = [r for r in ranked if r[2] >= PRESENT_WEIGHT]
    rivals = [r for r in ranked[1:] if not (w1 >= PRESENT_WEIGHT and r[2] >= PRESENT_WEIGHT)]
    w2 = rivals[0][2] if rivals else 0.0
    penalty = 0.3 if (rule_intent is not None and rule_intent != top_intent) else 0.0
    uncertainty = round(min(1.0, (1.0 - (w1 - w2)) + penalty), 6)
    if rivals and round(w1 - w2, 6) < RIVAL_GAP:
        return TriageResult([], True, uncertainty, (top_intent, rivals[0][1]))
    chosen = present if w1 >= PRESENT_WEIGHT else [ranked[0]]
    return TriageResult([IntentRequest(i, pid, w) for pid, i, w in chosen], False, uncertainty)
