"""Technician assignment and scoring (#109).

HARD RULES (never relaxed, never scored around): the technician has the SKILL for the issue, the PARTS the fix needs, the job falls in their SHIFT, they
are under their daily job cap, and, when the merchant speaks only one language, they speak it. A job nobody passes is returned UNASSIGNED with the reason
per technician; it is never given to the nearest person.

SCORE (among those who pass): fix rate for THIS issue type + continuity + durability - workload, with speed a small tie-break weight.
Durability (fixes that stayed fixed) weighs more than speed, so a quick technician whose repairs come back loses to a slower one whose repairs hold.
Case-mix adjustment: fix rate is taken per issue type and shrunk toward the team's rate for that same issue type, so a technician who was handed hard
jobs is not marked down against one who only got easy ones, and one who did two easy jobs is not marked up.
No coaching data is read here and no protected trait is used (``assert_no_protected_fields``).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Sequence

from field.profiles import TechProfile, assert_no_protected_fields, load_techs, shrunk_win_rate

SCHEMA_SQL = ("CREATE TABLE IF NOT EXISTS tech_history (tech_id TEXT NOT NULL, issue_type TEXT NOT NULL, jobs INTEGER NOT NULL, fixed INTEGER NOT NULL, "
              "durable INTEGER NOT NULL, avg_minutes INTEGER NOT NULL, PRIMARY KEY (tech_id, issue_type))",)
SPEED_REF_MIN = 60


@dataclass(frozen=True)
class Job:
    job_id: str
    merchant_id: str
    issue_type: str
    skill: str
    needed_parts: dict = field(default_factory=dict)
    language: str = "hi-IN"
    language_only: bool = False
    window: tuple[int, int] = (0, 24 * 60)
    owner_tech: str | None = None             # continuity
    point: tuple[float, float] | None = None
    priority: int = 0                         # higher first (an SLA about to breach)


@dataclass(frozen=True)
class IssueHistory:
    jobs: int
    fixed: int
    durable: int                              # fixed jobs with no repeat visit inside the durability window
    avg_minutes: int


@dataclass(frozen=True)
class AssignWeights:
    fix_rate: float = 10.0
    continuity: float = 2.0
    durability: float = 8.0
    speed: float = 1.0                        # deliberately far below durability
    workload: float = 1.0


@dataclass
class Assignment:
    job_id: str
    tech_id: str | None
    score: float | None = None
    parts: dict = field(default_factory=dict)
    reason: str = "ASSIGNED"
    rejected: dict = field(default_factory=dict)


def ensure_schema(conn: sqlite3.Connection) -> None:
    for s in SCHEMA_SQL:
        conn.execute(s)


def record_history(conn: sqlite3.Connection, tech_id: str, issue_type: str, *, jobs: int, fixed: int, durable: int, avg_minutes: int) -> None:
    if not (0 <= durable <= fixed <= jobs):
        raise ValueError("durable <= fixed <= jobs")
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO tech_history VALUES (?,?,?,?,?,?)", (tech_id, issue_type, jobs, fixed, durable, avg_minutes))


def load_history(conn: sqlite3.Connection) -> dict[tuple[str, str], IssueHistory]:
    ensure_schema(conn)
    return {(r["tech_id"], r["issue_type"]): IssueHistory(r["jobs"], r["fixed"], r["durable"], r["avg_minutes"]) for r in conn.execute("SELECT * FROM tech_history")}


def team_rates(history: dict, issue: str) -> tuple[float, float]:
    """(fix rate, durability rate) of the whole team on one issue type: the baseline each person is shrunk toward."""
    rows = [h for (t, i), h in history.items() if i == issue]
    jobs, fixed, durable = sum(h.jobs for h in rows), sum(h.fixed for h in rows), sum(h.durable for h in rows)
    return (fixed / jobs if jobs else 0.5), (durable / fixed if fixed else 0.5)


def hard_rule_failures(t: TechProfile, job: Job, assigned: int) -> list[str]:
    out = []
    if job.skill not in t.skills:
        out.append("SKILL")
    if not t.has_parts(job.needed_parts):
        out.append("PARTS")
    if job.language_only and not t.speaks(job.language, "fluent"):
        out.append("LANGUAGE")
    if max(t.shift[0], job.window[0]) >= min(t.shift[1], job.window[1]):
        out.append("SHIFT")
    if assigned >= t.max_jobs_per_day:
        out.append("DAILY_CAP")
    return out


def score_tech(t: TechProfile, job: Job, history: dict, assigned: int, w: AssignWeights) -> tuple[float, dict]:
    h = history.get((t.tech_id, job.issue_type), IssueHistory(0, 0, 0, SPEED_REF_MIN))
    base_fix, base_dur = team_rates(history, job.issue_type)
    fix = shrunk_win_rate(h.fixed / h.jobs if h.jobs else 0.0, h.jobs, base_fix)
    dur = shrunk_win_rate(h.durable / h.fixed if h.fixed else 0.0, h.fixed, base_dur)
    speed = max(0.0, min(1.0, SPEED_REF_MIN / max(h.avg_minutes, 1) - 0.5)) if h.jobs else 0.5
    parts = {"fix_rate": w.fix_rate * fix, "continuity": w.continuity * (job.owner_tech == t.tech_id), "durability": w.durability * dur,
             "speed": w.speed * speed, "workload": -w.workload * assigned}
    return round(sum(parts.values()), 6), {k: round(float(v), 6) for k, v in parts.items()}


def assign_jobs(techs: Sequence[TechProfile], jobs: Sequence[Job], history: dict, *, weights: AssignWeights | None = None) -> list[Assignment]:
    w = weights or AssignWeights()
    pool = {t.tech_id: t for t in techs}                  # parts are used up as jobs are given out
    assigned = {t.tech_id: 0 for t in techs}
    out = []
    for job in sorted(jobs, key=lambda j: (-j.priority, j.job_id)):
        best, rejected = None, {}
        for t in sorted(pool.values(), key=lambda t: t.tech_id):
            bad = hard_rule_failures(t, job, assigned[t.tech_id])
            if bad:
                rejected[t.tech_id] = bad[0]
                continue
            s, parts = score_tech(t, job, history, assigned[t.tech_id], w)
            if best is None or s > best[0]:
                best = (s, parts, t.tech_id)
        if best is None:
            out.append(Assignment(job.job_id, None, reason="NO_ELIGIBLE_TECHNICIAN", rejected=rejected))
        else:
            assigned[best[2]] += 1
            t = pool[best[2]]
            pool[t.tech_id] = replace(t, parts={**t.parts, **{k: t.parts[k] - n for k, n in job.needed_parts.items()}})
            out.append(Assignment(job.job_id, best[2], best[0], best[1], rejected=rejected))
    return out


def assign_from_db(conn: sqlite3.Connection, jobs: Sequence[Job], **kw) -> list[Assignment]:
    assert_no_protected_fields(conn)
    return assign_jobs(load_techs(conn), jobs, load_history(conn), **kw)
