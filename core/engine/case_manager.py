"""Lanes, focus and global fix-first (Bible section 14).

Fix-first is enforced by *absence*: ``allowed_actions`` never contains an offer action while a P0/P1
case is open, and ``intent_allowed`` is the engine's last gate before the executor.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from core.actions.registry import ActionRegistry
from core.engine.cases import LIVE, TERMINAL, Case, CaseRepo

LANES = ("P0", "P1", "P2", "P3")
RANK = {l: i for i, l in enumerate(LANES)}  # lower = more urgent
FIX_FIRST_LANES = frozenset({"P0", "P1"})
FIX_FIRST_RULE = "FIX-FIRST-001"


def _open(c: Any) -> bool:
    return getattr(c, "state", "ACTIVE") not in TERMINAL


def fix_first_active(cases_or_lanes: Iterable[Any]) -> bool:
    """True while any P0/P1 case is open. Accepts Case objects or bare lane strings."""
    for c in cases_or_lanes:
        if isinstance(c, str):
            if c in FIX_FIRST_LANES:
                return True
        elif _open(c) and c.lane in FIX_FIRST_LANES:
            return True
    return False


def _action_names(allowed: Any) -> list[str]:
    out: list[str] = []
    for a in allowed or []:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            out.extend(a.keys() if "name" not in a else [a["name"]])
        else:
            out.append(getattr(a, "name"))
    return out


def allowed_actions(case: Any, step_playbook: Any, merchant_open_cases: Iterable[Any], registry: ActionRegistry) -> list[str]:
    """The playbook's registered actions, minus every offer action while any P0/P1 case is open."""
    names = [n for n in _action_names(getattr(step_playbook, "allowed_actions", [])) if registry.has(n) and registry.get(n).agent_callable]
    if fix_first_active(list(merchant_open_cases)):
        offers = registry.offer_actions()
        names = [n for n in names if n not in offers]
    return list(dict.fromkeys(names))


@dataclass(frozen=True)
class Verdict:
    ok: bool
    rule_id: str | None = None
    reason: str | None = None


def intent_allowed(action: str, allowlist: Iterable[str], open_case_lanes: Iterable[str], registry: ActionRegistry,
                   *, case_lane: str | None = None, proactive: bool = False) -> Verdict:
    """Last gate before the executor. Rejects an offer action (or proactive P3 outreach) under fix-first,
    and anything that is not on the computed allowlist."""
    if not registry.has(action):
        return Verdict(False, "UNKNOWN-ACTION", f"{action} is not a registered action")
    ff = fix_first_active(list(open_case_lanes))
    if ff and action in registry.offer_actions():
        return Verdict(False, FIX_FIRST_RULE, f"{action} is an offer action and a P0/P1 case is open")
    if ff and proactive and case_lane == "P3":
        return Verdict(False, FIX_FIRST_RULE, "proactive sales outreach is suppressed while a P0/P1 case is open")
    if action not in set(allowlist):
        return Verdict(False, "NOT-ALLOWLISTED", f"{action} is not allowed at this step")
    return Verdict(True)


@dataclass
class FocusResult:
    focus: Case | None
    parked: list[Case]


class CaseManager:
    def __init__(self, repo: CaseRepo, registry: ActionRegistry):
        self.repo, self.registry = repo, registry

    def suppress_proactive_sales(self, merchant_id: str) -> bool:
        return fix_first_active(self.repo.open_cases(merchant_id))

    def allowed_actions(self, case: Case, playbook: Any, merchant_open_cases: Iterable[Case] | None = None) -> list[str]:
        cases = list(merchant_open_cases) if merchant_open_cases is not None else self.repo.open_cases(case.subject_id)
        return allowed_actions(case, playbook, cases, self.registry)

    def intent_allowed(self, action: str, allowlist: Iterable[str], open_case_lanes: Iterable[str], *,
                       case_lane: str | None = None, proactive: bool = False) -> Verdict:
        return intent_allowed(action, allowlist, open_case_lanes, self.registry, case_lane=case_lane, proactive=proactive)

    # -- focus ---------------------------------------------------------------------
    def _key(self, c: Case) -> tuple:
        # highest lane first (lowest rank), then most recent activity, then newest id
        return (-RANK.get(c.lane, 9), self.repo.last_activity(c.case_id), c.case_id)

    def park(self, case: Case, note: str) -> Case:
        if case.state == "PARKED":
            return case
        return self.repo.transition(case, "PARKED", reason="parked", resume_note=note)

    def focus(self, merchant_id: str, touched_case_ids: Iterable[str]) -> FocusResult:
        """Highest lane among the cases the latest message touches and the case(s) being worked now;
        ties go to the most recent. Every other live case is parked with a one-line resume note."""
        touched = set(touched_case_ids)
        cands = [c for c in self.repo.open_cases(merchant_id)
                 if c.state in LIVE or (c.case_id in touched and c.state in ("PARKED", "NOT_NOW"))]
        if not cands:
            return FocusResult(None, [])
        winner = max(cands, key=self._key)
        parked: list[Case] = []
        for c in cands:
            if c.case_id == winner.case_id or c.state not in LIVE:
                continue
            if c.lane == "P3" and winner.lane in FIX_FIRST_LANES:
                note = f"I'll come back to {c.playbook_id} once {winner.playbook_id} is sorted."
            else:
                note = f"Resume {c.playbook_id} at step {c.step or 'start'} after {winner.playbook_id}."
            parked.append(self.park(c, note))
        if winner.state in ("PARKED", "NOT_NOW"):
            winner = self.repo.transition(winner, "ACTIVE", reason="focus")
        return FocusResult(self.repo.load(winner.case_id), parked)

    def release_parked(self, merchant_id: str) -> list[Case]:
        """After a case closes: bring back the best parked case, unless fix-first still holds it back
        or an equal/higher-lane case is already being worked."""
        cases = self.repo.open_cases(merchant_id)
        parked = [c for c in cases if c.state == "PARKED"]
        if not parked:
            return []
        best = max(parked, key=self._key)
        others = [c for c in cases if c.case_id != best.case_id]
        if best.lane not in FIX_FIRST_LANES and fix_first_active(others):
            return []
        if any(c.state in LIVE and RANK.get(c.lane, 9) <= RANK.get(best.lane, 9) for c in others):
            return []
        return [self.repo.transition(best, "ACTIVE", reason="unparked")]
