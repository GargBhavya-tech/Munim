"""Rep matching (#108): which rep gets which lead.

Everything here is computed by code from system reads (profiles, lead rows, injected travel times). No model is involved and no number is invented.

HARD RULES (a candidate that fails one is never scored, never a fallback):
  * language: a ``language_only`` merchant (speaks only that language) goes only to a rep who speaks it at least fluently. Never a preference.
  * training: the rep is trained for the product the lead is about.
  * stock: a lead that needs a physical product (soundbox, card machine) needs it on the rep.
  * shift + territory: the rep's shift overlaps the lead's visit window, and the road time from the rep's base is within the territory radius.
  * workload cap: a rep never takes more than ``max_leads`` in one run.

SCORE (only among those who pass): language fit + segment win rate (Bayesian-shrunk, from reps' own system-derived rates) + continuity (the rep who
already owns the merchant) + seniority-to-value (senior reps for high-value leads) - workload. A lead is never "fairer" for anyone's community or
religion: ``assert_no_protected_fields`` runs on every call. Coaching notes are NOT an input: this module does not read them, so no score, rank or
penalty can derive from coaching (privacy #113).

Fair spread: leads are taken highest value first and each rep's already assigned load lowers their score, so one rep is not given every good lead.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

from field.profiles import LEVELS, RepProfile, assert_no_protected_fields, load_reps, shrunk_win_rate, team_prior

NEEDS_STOCK = frozenset({"soundbox", "card_machine"})
HIGH_VALUE_PAISE = 5_000_000        # a lead worth at least this (expected value, paise) wants a senior rep
SENIOR_MONTHS = 24
DEFAULT_VISITS = 30      # evidence behind a profile win rate when the caller has no per-segment visit count


@dataclass(frozen=True)
class LeadNeed:
    lead_id: str
    segment: str                    # restaurant | kirana | chemist | d2c ...
    product: str                    # soundbox | card_machine | loans ...
    language: str                   # the merchant's language, e.g. kn-IN
    value_paise: int                # expected value, from the scoring job (a system read)
    point: tuple[float, float]
    language_only: bool = False     # the merchant speaks only this language
    window: tuple[int, int] = (0, 24 * 60)      # minutes of the day the visit can happen
    owner_rep: str | None = None    # continuity: the rep who already knows the merchant


@dataclass(frozen=True)
class Weights:
    language: float = 3.0
    win_rate: float = 10.0
    continuity: float = 2.0
    seniority: float = 1.5
    workload: float = 1.0


@dataclass
class MatchResult:
    lead_id: str
    rep_id: str | None
    score: float | None = None
    parts: dict = field(default_factory=dict)
    reason: str = "ASSIGNED"        # ASSIGNED | NO_ELIGIBLE_REP
    rejected: dict = field(default_factory=dict)       # rep_id -> hard rule that excluded them


def hard_rule_failures(rep: RepProfile, lead: LeadNeed, travel_min: Callable[[tuple, tuple], float], max_travel_min: float, assigned: int, max_leads: int) -> list[str]:
    out = []
    if lead.language_only and not rep.speaks(lead.language, "fluent"):
        out.append("LANGUAGE")
    if not rep.trained_for(lead.product):
        out.append("TRAINING")
    if lead.product in NEEDS_STOCK and not rep.in_stock(lead.product):
        out.append("STOCK")
    if max(rep.shift[0], lead.window[0]) >= min(rep.shift[1], lead.window[1]):
        out.append("SHIFT")
    if travel_min(rep.base, lead.point) > max_travel_min:
        out.append("TERRITORY")
    if assigned >= max_leads:
        out.append("WORKLOAD_CAP")
    return out


def score_candidate(rep: RepProfile, lead: LeadNeed, *, prior: float, visits: int, assigned: int, w: Weights) -> tuple[float, dict]:
    lang = LEVELS.get(rep.languages.get(lead.language, ""), 0) / 3            # 0, 1/3, 2/3, 1
    raw = rep.win_rates.get(lead.segment)
    win = shrunk_win_rate(raw if raw is not None else prior, visits if raw is not None else 0, prior)
    cont = 1.0 if lead.owner_rep == rep.rep_id else 0.0
    senior = min(rep.experience_months, SENIOR_MONTHS) / SENIOR_MONTHS
    want = 1.0 if lead.value_paise >= HIGH_VALUE_PAISE else 0.0
    fit = 1 - abs(senior - want) if want else 1 - 0.5 * senior                   # high value wants seniority; low value mildly keeps seniors free
    parts = {"language": w.language * lang, "win_rate": w.win_rate * win, "continuity": w.continuity * cont,
             "seniority": w.seniority * fit, "workload": -w.workload * assigned}
    return round(sum(parts.values()), 6), {k: round(v, 6) for k, v in parts.items()}


def match_leads(reps: Sequence[RepProfile], leads: Sequence[LeadNeed], travel_min: Callable[[tuple, tuple], float], *, max_travel_min: float = 60,
                max_leads: int = 8, weights: Weights | None = None, visit_counts: dict[tuple[str, str], int] | None = None) -> list[MatchResult]:
    w, visit_counts = weights or Weights(), visit_counts or {}
    pool = {r.rep_id: r for r in reps}                    # stock is used up as leads are given out
    assigned = {r.rep_id: 0 for r in reps}
    results = []
    for lead in sorted(leads, key=lambda l: (-l.value_paise, l.lead_id)):
        prior = team_prior(list(reps), lead.segment)
        best, rejected = None, {}
        for rep in sorted(pool.values(), key=lambda r: r.rep_id):
            bad = hard_rule_failures(rep, lead, travel_min, max_travel_min, assigned[rep.rep_id], max_leads)
            if bad:
                rejected[rep.rep_id] = bad[0]
                continue
            s, parts = score_candidate(rep, lead, prior=prior, visits=visit_counts.get((rep.rep_id, lead.segment), DEFAULT_VISITS), assigned=assigned[rep.rep_id], w=w)
            if best is None or s > best[0]:
                best = (s, parts, rep.rep_id)
        if best is None:
            results.append(MatchResult(lead.lead_id, None, reason="NO_ELIGIBLE_REP", rejected=rejected))      # left for a person; never relaxed
        else:
            assigned[best[2]] += 1
            if lead.product in NEEDS_STOCK:
                r = pool[best[2]]
                pool[r.rep_id] = replace(r, stock={**r.stock, lead.product: r.stock[lead.product] - 1})
            results.append(MatchResult(lead.lead_id, best[2], best[0], best[1], rejected=rejected))
    return results


def match_from_db(conn: sqlite3.Connection, leads: Sequence[LeadNeed], travel_min, **kw) -> list[MatchResult]:
    assert_no_protected_fields(conn)
    return match_leads(load_reps(conn), leads, travel_min, **kw)
