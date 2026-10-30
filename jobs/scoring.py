"""Turnover estimate and lead scoring (#99; Bible section 9). Formula first, a learned model second, uncertainty always shown.

  value = expected monthly volume x P(switch) x P(later loan) / cost to acquire        (the formula; every constant is an [ASSUMPTION])
  turnover  from the signals a lead really has: GST slab, category ticket size, area busyness (payments in its pincode), review volume.
            ONE signal -> flagged ``uncertain`` and the estimate is a wide band; two or more narrow it.
  model     logistic regression (numpy), retrained weekly on SYNTHETIC conversions. It predicts P(switch) and replaces the formula's prior once it
            has enough matured labels. Features are a WHITELIST of things known when the lead was scored; anything only known after conversion
            (first settlement volume, days to activate) is refused, because training on it is the leakage that makes an AUC look perfect.
  evaluation  walk-forward by week: train on weeks before w, test on week w; the reported AUC is on LATER weeks only.
Rows with a lead's conversion label are used only once the label has matured (``conversion_window_days`` after scoring), never earlier.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Sequence

import numpy as np

from core.engine.clock import SimClock, iso, parse_iso

SLAB_MONTHLY_PAISE = {"<40L": 150_000_00, "40L-1.5Cr": 600_000_00, "1.5Cr-5Cr": 2_000_000_00, ">5Cr": 6_000_000_00}   # monthly card+UPI volume, mid-band [ASSUMPTION]
SLAB_ORDINAL = {"<40L": 0, "40L-1.5Cr": 1, "1.5Cr-5Cr": 2, ">5Cr": 3}
CATEGORY_TICKET = {"restaurant": 1.0, "pharmacy": 1.1, "kirana": 0.9, "salon": 0.8, "electronics": 1.6, "bakery": 0.8, "d2c": 1.3}   # relative ticket size [ASSUMPTION]
P_SWITCH_PRIOR = 0.12
P_LOAN_PRIOR = {"restaurant": 0.10, "pharmacy": 0.12, "kirana": 0.15, "salon": 0.08, "electronics": 0.14, "bakery": 0.09, "d2c": 0.11}
CAC_PAISE = {"field_rep": 250_000, "ai_voice": 40_000, "self_serve": 8_000}      # cost to acquire by channel [ASSUMPTION]
CONVERSION_WINDOW_DAYS = 14
MIN_TRAIN_ROWS = 60
LEAKY = frozenset({"first_settlement_paise", "days_to_activate", "activated", "converted_at", "loan_taken"})   # "converted" is the LABEL, never a feature


class LeakageError(ValueError):
    pass


@dataclass(frozen=True)
class Turnover:
    monthly_paise: int
    low_paise: int
    high_paise: int
    signals: tuple[str, ...]

    @property
    def uncertain(self) -> bool:
        return len(self.signals) < 2


def estimate_turnover(slab: str | None, category: str | None, area_busyness: float | None, reviews: int | None = None) -> Turnover | None:
    """None when there is no signal at all. ``area_busyness`` is 0..1 (the pincode's rank by 30-day payments)."""
    signals, mid = [], None
    if slab in SLAB_MONTHLY_PAISE:
        signals.append("gst_slab")
        mid = SLAB_MONTHLY_PAISE[slab]
    if category in CATEGORY_TICKET:
        signals.append("category")
        mid = (mid or SLAB_MONTHLY_PAISE["<40L"] * 2) * CATEGORY_TICKET[category] if mid is None else mid * CATEGORY_TICKET[category]
    if area_busyness is not None:
        signals.append("area_busyness")
        mid = (mid or SLAB_MONTHLY_PAISE["<40L"] * 2) * (0.8 + 0.4 * float(area_busyness))
    if reviews is not None:
        signals.append("reviews")
        mid = (mid or SLAB_MONTHLY_PAISE["<40L"] * 2) * (1.0 + min(int(reviews), 500) / 1000)
    if not signals or mid is None:
        return None
    width = {1: 0.6, 2: 0.35}.get(len(signals), 0.2)                       # more independent signals, a narrower band
    return Turnover(int(mid), int(mid * (1 - width)), int(mid * (1 + width)), tuple(signals))


def formula_value(turnover: Turnover, category: str | None, channel: str = "ai_voice", p_switch: float = P_SWITCH_PRIOR) -> int:
    """Expected monthly volume x P(switch) x P(later loan) / cost to acquire, in paise-of-volume per paise of cost."""
    p_loan = P_LOAN_PRIOR.get(category or "", 0.1)
    return int(turnover.monthly_paise * p_switch * p_loan / CAC_PAISE[channel])


# --------------------------------------------------------------------------------------------------------------- the learned part
def features(row: dict) -> np.ndarray:
    """Only whitelisted, scoring-time features. Refuses a row that carries anything known only after conversion."""
    bad = LEAKY & set(row)
    if bad:
        raise LeakageError(f"post-conversion fields in a training row: {sorted(bad)}")
    return np.array([SLAB_ORDINAL.get(row.get("slab"), 0) / 3, CATEGORY_TICKET.get(row.get("category"), 1.0) - 1.0, float(row.get("area_busyness", 0.5)),
                     float(row.get("confidence", 0.7)), float(row.get("review_score", 0.5)),
                     float(row.get("category") == "kirana"), float(row.get("category") == "restaurant"), float(row.get("category") == "pharmacy")])


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class Logistic:
    """L2-regularised logistic regression by Newton's method (deterministic: no random start)."""

    def __init__(self, l2: float = 1.0, iters: int = 25):
        self.l2, self.iters, self.w = l2, iters, None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "Logistic":
        Xb = np.hstack([np.ones((len(X), 1)), X])
        w = np.zeros(Xb.shape[1])
        reg = self.l2 * np.eye(len(w))
        reg[0, 0] = 0.0
        for _ in range(self.iters):
            p = _sigmoid(Xb @ w)
            g = Xb.T @ (p - y) + reg @ w
            H = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg
            step = np.linalg.solve(H + 1e-9 * np.eye(len(w)), g)
            w -= step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.w = w
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return _sigmoid(np.hstack([np.ones((len(X), 1)), X]) @ self.w)


def auc(y: Sequence[int], score: Sequence[float]) -> float | None:
    """Rank-based AUC with ties averaged; None when only one class is present."""
    y, s = np.asarray(y), np.asarray(score)
    pos, neg = int((y == 1).sum()), int((y == 0).sum())
    if pos == 0 or neg == 0:
        return None
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    i = 0
    ss = s[order]
    while i < len(s):
        j = i
        while j + 1 < len(s) and ss[j + 1] == ss[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def matured(rows: Iterable[dict], as_of_week: int) -> list[dict]:
    """Rows whose label is KNOWN at the start of ``as_of_week``: scored at least one conversion window before it."""
    lag = math.ceil(CONVERSION_WINDOW_DAYS / 7)
    return [r for r in rows if r["week"] + lag <= as_of_week]


def walk_forward(rows: list[dict], first_test_week: int | None = None) -> dict:
    """Train on matured weeks before w, test on week w, for every later week. The headline AUC is the pooled score on later weeks only."""
    weeks = sorted({r["week"] for r in rows})
    first = first_test_week if first_test_week is not None else weeks[len(weeks) // 2]
    per_week, ys, ps = {}, [], []
    for w in [x for x in weeks if x >= first]:
        train = matured(rows, w)
        test = [r for r in rows if r["week"] == w]
        if len(train) < MIN_TRAIN_ROWS or not test:
            continue
        m = Logistic().fit(np.array([features(r) for r in train]), np.array([r["converted"] for r in train], float))
        p = m.predict(np.array([features(r) for r in test]))
        per_week[w] = auc([r["converted"] for r in test], p)
        ys += [r["converted"] for r in test]
        ps += list(p)
    return {"per_week": per_week, "auc_later_weeks": auc(ys, ps), "tested_rows": len(ys), "first_test_week": first}


# --------------------------------------------------------------------------------------------------------------- scoring leads
def area_busyness(conn: sqlite3.Connection, clock: SimClock) -> dict[str, float]:
    """pincode -> 0..1 rank of its merchants' 30-day successful payment volume."""
    since = iso(clock.now() - timedelta(days=30))
    vol = {r["pincode"]: r["v"] for r in conn.execute(
        "SELECT m.pincode, COALESCE(SUM(p.amount_paise), 0) v FROM merchants m LEFT JOIN payments p ON p.merchant_id = m.merchant_id "
        "AND p.status = 'SUCCESS' AND p.ts >= ? GROUP BY m.pincode", (since,))}
    order = sorted(vol, key=lambda k: (vol[k], k))
    return {pin: (i / (len(order) - 1) if len(order) > 1 else 0.5) for i, pin in enumerate(order)}


@dataclass(frozen=True)
class ScoredLead:
    lead_id: str
    value: int
    turnover: Turnover | None
    uncertain: bool
    p_switch: float
    channel: str


def channel_for(value: int) -> str:
    """High value -> a field rep with a planned route; mid -> AI voice / WhatsApp; low -> a self-serve link (Bible section 9)."""
    return "field_rep" if value >= 6 else "ai_voice" if value >= 1 else "self_serve"


def score_ready_leads(conn: sqlite3.Connection, clock: SimClock, model: Logistic | None = None) -> list[ScoredLead]:
    """Scores only leads that dedupe has cleared (nothing else may be pitched), writes ``leads.score`` and returns them best first.
    With a trained ``model`` P(switch) comes from it; otherwise from the prior."""
    from jobs.dedupe import outreach_ready
    busy = area_busyness(conn, clock)
    out = []
    for r in outreach_ready(conn):
        t = estimate_turnover(r["gst_slab"], r["category"], busy.get(r["pincode"]))
        if t is None:
            continue
        p_switch = P_SWITCH_PRIOR
        if model is not None:
            p_switch = float(model.predict(features({"slab": r["gst_slab"], "category": r["category"], "area_busyness": busy.get(r["pincode"], 0.5),
                                                      "confidence": r["confidence"] or 0.7}).reshape(1, -1))[0])
        value = formula_value(t, r["category"], "ai_voice", p_switch)
        out.append(ScoredLead(r["lead_id"], value, t, t.uncertain, p_switch, channel_for(value)))
    out.sort(key=lambda s: (-s.value, s.lead_id))
    for s in out:
        conn.execute("UPDATE leads SET score = ? WHERE lead_id = ?", (float(s.value), s.lead_id))
    return out


# --------------------------------------------------------------------------------------------------------------- synthetic conversions
def synthetic_conversions(weeks: int = 12, per_week: int = 60, seed: int = 7, leak: bool = False) -> list[dict]:
    """Deterministic SYNTHETIC history: leads scored in week w, converted (or not) later. The true switch probability depends on slab, category
    and area busyness, so a model that avoids leakage can recover it. ``leak=True`` adds post-conversion fields to show the guard refuses them."""
    rng = np.random.default_rng(seed)
    cats = list(CATEGORY_TICKET)
    rows = []
    for w in range(weeks):
        for _ in range(per_week):
            slab = list(SLAB_ORDINAL)[int(rng.integers(0, 4))]
            cat = cats[int(rng.integers(0, len(cats)))]
            busy, conf, rev = float(rng.random()), float(0.5 + 0.45 * rng.random()), float(rng.random())
            z = -2.2 + 1.1 * SLAB_ORDINAL[slab] / 3 * 3 / 3 + 0.9 * (CATEGORY_TICKET[cat] - 1) + 1.4 * (busy - 0.5) + 0.5 * (cat == "kirana")
            conv = int(rng.random() < float(_sigmoid(np.array([z]))[0]))
            row = {"week": w, "slab": slab, "category": cat, "area_busyness": busy, "confidence": conf, "review_score": rev, "converted": conv}
            if leak:
                row["first_settlement_paise"] = int(conv) * 1000
            rows.append(row)
    return rows
