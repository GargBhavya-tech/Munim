"""Territory redesign and fault forecasts (#112; Bible step E9).

TERRITORIES are balanced by LOAD, not by shop count: each area's load is half its share of the total sales opportunity (expected value, paise) and half
its share of the total service work (minutes), so ten big-opportunity shops weigh more than forty dormant ones. ``redesign`` places areas heaviest first
on the lightest territory, then improves by moves and swaps while the spread (max - min load) shrinks. All of it is integer-in, deterministic and computed
by code; no model is involved.

FORECASTS are seasonal: the faults an area had in the same calendar month of earlier years, scaled by the year-on-year trend (clamped), per area. ``backtest``
holds out the last twelve months, forecasts them from the rest and REPORTS the mean absolute error next to a naive "same as last month" baseline, so the
forecast has to earn its place. STAFFING and VAN STOCK are integer arithmetic over the forecast (ceil), with the rates passed in from the parts/shift
system reads, never guessed. Coaching and protected traits are not inputs (``assert_no_protected_fields`` guards the DB entry point).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from statistics import mean

from field.profiles import assert_no_protected_fields

TREND_MIN, TREND_MAX = 0.5, 2.0


@dataclass(frozen=True)
class Area:
    area_id: str
    opportunity_paise: int
    service_minutes: int
    shops: int = 0                      # shown for comparison only; never part of the load


def area_loads(areas: list[Area]) -> dict[str, float]:
    to, ts = sum(a.opportunity_paise for a in areas) or 1, sum(a.service_minutes for a in areas) or 1
    return {a.area_id: 0.5 * a.opportunity_paise / to + 0.5 * a.service_minutes / ts for a in areas}


@dataclass
class Redesign:
    territories: list[list[str]]
    loads: list[float]
    spread: float

    @property
    def imbalance_ratio(self) -> float:
        return max(self.loads) / min(self.loads) if min(self.loads) else math.inf


def _spread(groups, load):
    t = [sum(load[a] for a in g) for g in groups]
    return max(t) - min(t), t


def redesign(areas: list[Area], n_territories: int, *, by: str = "load") -> Redesign:
    """by='load' is the real method; by='shops' exists so a test can show why shop count is the wrong thing to balance."""
    if n_territories < 1 or n_territories > len(areas):
        raise ValueError("territories must be between 1 and the number of areas")
    load = area_loads(areas)
    key = load if by == "load" else {a.area_id: float(a.shops) for a in areas}
    groups: list[list[str]] = [[] for _ in range(n_territories)]
    for a in sorted(areas, key=lambda a: (-key[a.area_id], a.area_id)):
        min(groups, key=lambda g: (sum(key[x] for x in g), len(g))).append(a.area_id)
    if by == "load":
        improved = True
        while improved:
            improved = False
            cur, _ = _spread(groups, key)
            for i, gi in enumerate(groups):
                for j, gj in enumerate(groups):
                    if i == j:
                        continue
                    for a in list(gi):
                        if len(gi) > 1:                                              # move a: i -> j
                            gi.remove(a), gj.append(a)
                            s, _ = _spread(groups, key)
                            if s < cur - 1e-12:
                                cur, improved = s, True
                                break
                            gj.remove(a), gi.append(a)
                        for b in list(gj):                                           # swap a <-> b
                            gi.remove(a), gj.remove(b), gi.append(b), gj.append(a)
                            s, _ = _spread(groups, key)
                            if s < cur - 1e-12:
                                cur, improved = s, True
                                break
                            gi.remove(b), gj.remove(a), gi.append(a), gj.append(b)
                        if improved:
                            break
                    if improved:
                        break
                if improved:
                    break
    groups = [sorted(g) for g in groups]
    spread, totals = _spread(groups, load)                                           # always REPORT the real load balance
    return Redesign(groups, totals, spread)


# ------------------------------------------------------------------------------------------------------ forecasts
def fault_history(conn: sqlite3.Connection, ticket_types: tuple[str, ...] | None = None) -> dict[str, dict[str, int]]:
    """area (pincode) -> {'YYYY-MM': faults}, from tickets joined to merchants. A system read."""
    assert_no_protected_fields(conn)
    q = "SELECT m.pincode AS area, substr(t.created_at, 1, 7) AS ym, COUNT(*) AS n FROM tickets t JOIN merchants m ON m.merchant_id = t.merchant_id"
    args: tuple = ()
    if ticket_types:
        q += " WHERE t.type IN (%s)" % ",".join("?" * len(ticket_types))
        args = ticket_types
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(q + " GROUP BY area, ym", args):
        out.setdefault(r["area"], {})[r["ym"]] = r["n"]
    return out


def _months_before(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:])
    i = y * 12 + (m - 1) - k
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def forecast_month(series: dict[str, int], target: str) -> float:
    """The same calendar month of earlier years, each carried forward by the year-on-year growth (clamped) measured at the latest month on record,
    averaged. Only data in ``series`` is used, so what the caller leaves out cannot leak in."""
    if not series:
        return 0.0
    origin = max(series)
    seasonal = [(k, series[_months_before(target, 12 * k)]) for k in range(1, 6) if _months_before(target, 12 * k) in series]
    if not seasonal:
        recent = [series[_months_before(origin, k)] for k in range(0, 3) if _months_before(origin, k) in series]
        return mean(recent) if recent else 0.0
    last = sum(series.get(_months_before(origin, k), 0) for k in range(0, 12))
    prev = sum(series.get(_months_before(origin, k), 0) for k in range(12, 24))
    g = min(TREND_MAX, max(TREND_MIN, last / prev)) if prev else 1.0
    return mean(v * g ** k for k, v in seasonal)


@dataclass
class Backtest:
    mae: float
    baseline_mae: float
    per_area: dict[str, float] = field(default_factory=dict)
    months: list[str] = field(default_factory=list)


def backtest(history: dict[str, dict[str, int]], holdout: list[str]) -> Backtest:
    """Forecast each held-out month from data strictly before it; report MAE overall and per area, and the naive last-month baseline's MAE."""
    errs, base, per = [], [], {}
    for area, series in sorted(history.items()):
        e = []
        for ym in holdout:
            past = {k: v for k, v in series.items() if k < holdout[0]}          # nothing from the holdout leaks into a forecast
            e.append(abs(forecast_month(past, ym) - series.get(ym, 0)))
            base.append(abs(past.get(_months_before(holdout[0], 1), 0) - series.get(ym, 0)))
        per[area] = round(mean(e), 4)
        errs += e
    return Backtest(round(mean(errs), 4), round(mean(base), 4), per, list(holdout))


def staffing(forecast_faults: float, minutes_per_fault: int, working_days: int, minutes_per_tech_day: int) -> int:
    need = math.ceil(forecast_faults) * minutes_per_fault
    cap = working_days * minutes_per_tech_day
    return -(-need // cap)


def van_stock(forecast_faults: float, parts_per_fault: dict[str, float], safety_pct: int = 10) -> dict[str, int]:
    """Parts to load: ceil(forecast * parts-per-fault * (100 + safety) / 100), integers throughout the rounding."""
    return {p: math.ceil(math.ceil(forecast_faults) * rate * (100 + safety_pct) / 100) for p, rate in sorted(parts_per_fault.items())}
