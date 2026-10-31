"""One ranked list for human time: merchants at risk of leaving, and new leads, in the same units.

A new lead's value (``jobs/scoring.formula_value``) is the expected monthly volume times the chance of winning them (and of a later loan) per paise of cost to win. A merchant at
risk of leaving is the same kind of number: the monthly volume that would walk away, times the chance they leave, times the chance a save works, per paise of cost to try.
Both are "paise of monthly volume won or kept per paise spent", so they sort together and the next hour of a rep's or an analyst's time goes where it earns most.

Only a persistent dip with no service explanation is retention work: a device that is offline or failing settlements is a service or support case, and an area-wide dip is not
the merchant's doing (the health job already says so). The chance of leaving by diagnosis and the chance a save works by approach are [ASSUMPTION]s on synthetic data and are
returned with every item. ``dry_run_health`` runs the health job on an in-memory COPY of the database, so looking at the list never emits an event or writes a row.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field

from core.engine.clock import SimClock
from jobs.approach import approach_for
from jobs.dedupe import outreach_ready
from jobs.health import COMPETITOR_SWITCH, UNEXPLAINED_DROP, ZERO_ACTIVITY, HealthJob, HealthReport
from jobs.scoring import CAC_PAISE, area_busyness, estimate_turnover

DAYS_PER_MONTH = 30
P_LOSE = {COMPETITOR_SWITCH: 0.5, UNEXPLAINED_DROP: 0.25, ZERO_ACTIVITY: 0.4}          # [ASSUMPTION] chance the merchant leaves, by what the dip turned out to be
P_SAVE = {"ai_voice": 0.45, "field_rep": 0.7}                                          # [ASSUMPTION] chance one save attempt works, by approach
RETENTION_DIAGNOSES = tuple(P_LOSE)


@dataclass(frozen=True)
class WorkItem:
    kind: str                          # merchant_at_risk | new_lead
    item_id: str
    name: str
    value: int
    channel: str | None
    why: tuple[str, ...]
    monthly_paise: int | None = None
    diagnosis: str | None = None
    p: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def dry_run_health(conn: sqlite3.Connection, clock: SimClock, **kw) -> HealthReport:
    """The health job on a throwaway in-memory copy: nothing is emitted into, or stored in, the real database."""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    try:
        conn.backup(mem)
        return HealthJob(mem, clock, **kw).run()
    finally:
        mem.close()


def at_risk_value(monthly_paise: int, p_lose: float, p_save: float, cost_paise: int) -> int:
    return int(monthly_paise * p_lose * p_save / cost_paise)


def merchant_items(conn: sqlite3.Connection, report: HealthReport) -> list[WorkItem]:
    out = []
    for f in report.findings:
        if f.diagnosis not in P_LOSE:
            continue
        m = conn.execute("SELECT name, avg_daily_volume_paise FROM merchants WHERE merchant_id = ?", (f.merchant_id,)).fetchone()
        if m is None:
            continue
        monthly = int(m["avg_daily_volume_paise"] or 0) * DAYS_PER_MONTH
        best = max(P_SAVE, key=lambda c: (at_risk_value(monthly, P_LOSE[f.diagnosis], P_SAVE[c], CAC_PAISE[c]), c))
        v = at_risk_value(monthly, P_LOSE[f.diagnosis], P_SAVE[best], CAC_PAISE[best])
        out.append(WorkItem("merchant_at_risk", f.merchant_id, m["name"], v, best, (f"The dip is real and persistent, and it is {f.diagnosis.replace('_', ' ').lower()}",),
                            monthly, f.diagnosis, {"p_lose": P_LOSE[f.diagnosis], "p_save": P_SAVE[best], "cost_paise": CAC_PAISE[best]}))
    return out


def lead_items(conn: sqlite3.Connection, clock: SimClock) -> list[WorkItem]:
    busy = area_busyness(conn, clock)
    out = []
    for r in outreach_ready(conn):
        if r["score"] is None:
            continue
        t = estimate_turnover(r["gst_slab"], r["category"], busy.get(r["pincode"]))
        a = approach_for(int(r["score"]), uncertain=t.uncertain if t else True, category=r["category"])
        out.append(WorkItem("new_lead", r["lead_id"], r["name"], int(r["score"]), a.level, a.reasons, t.monthly_paise if t else None))
    return out


def ranked_work(conn: sqlite3.Connection, clock: SimClock, report: HealthReport) -> list[WorkItem]:
    """Everything worth a person's time, best first. Ties go to the id, so the order is the same every time."""
    items = merchant_items(conn, report) + lead_items(conn, clock)
    return sorted(items, key=lambda w: (-w.value, w.kind, w.item_id))
