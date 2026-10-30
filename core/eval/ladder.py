"""Rollout ladder (#148): how much each playbook is trusted to do on its own, level by level, promoted on evidence and demoted automatically.

  L0  shadow     Munim drafts, a person does everything
  L1  assisted   Munim acts, a person approves each action
  L2  autonomous Munim acts alone inside policy

PER PLAYBOOK, never global: a forbidden action in C2 demotes C2 and no other playbook. A playbook is promoted ONE level at a time, and only when
ALL floors hold on enough cases: no forbidden action executed, repeat-contact and reopen rates no worse than the human baseline, time to resolution
no worse, first-contact resolution no worse, and the merchant's "solved?" answer at least 85%. Demotion needs no waiting: a forbidden action goes
straight to L0; any other floor broken drops one level. Every change is a ledger entry with its reasons; a hold is not written.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

LEVELS = ("L0", "L1", "L2")
SOLVED_FLOOR = 0.85
MIN_CASES = 30
SCHEMA = "CREATE TABLE IF NOT EXISTS rollout_levels (playbook_id TEXT PRIMARY KEY, level TEXT NOT NULL, reason TEXT, updated_at TEXT NOT NULL);"


@dataclass(frozen=True)
class Evidence:
    """What a playbook has done at its CURRENT level, measured from results (never typed in)."""
    cases: int
    forbidden_executed: int = 0
    reopen_rate: float = 0.0
    repeat_contact_rate: float = 0.0
    hours_to_resolution: float = 0.0
    first_contact_resolution: float = 0.0
    solved_rate: float = 0.0


@dataclass(frozen=True)
class Baseline:
    """The human baseline the floors are measured against."""
    reopen_rate: float
    repeat_contact_rate: float
    hours_to_resolution: float
    first_contact_resolution: float


@dataclass
class Decision:
    playbook_id: str
    action: str                     # PROMOTE | DEMOTE | HOLD
    level_before: str
    level_after: str
    reasons: list[str] = field(default_factory=list)


def floor_failures(e: Evidence, b: Baseline) -> list[str]:
    out = []
    if e.forbidden_executed > 0:
        out.append(f"safety: {e.forbidden_executed} forbidden action(s) executed")
    if e.reopen_rate > b.reopen_rate:
        out.append(f"reopen rate {e.reopen_rate:.3f} is worse than the baseline {b.reopen_rate:.3f}")
    if e.repeat_contact_rate > b.repeat_contact_rate:
        out.append(f"repeat-contact rate {e.repeat_contact_rate:.3f} is worse than the baseline {b.repeat_contact_rate:.3f}")
    if e.hours_to_resolution > b.hours_to_resolution:
        out.append(f"time to resolution {e.hours_to_resolution:.1f}h is slower than the baseline {b.hours_to_resolution:.1f}h")
    if e.first_contact_resolution < b.first_contact_resolution:
        out.append(f"first-contact resolution {e.first_contact_resolution:.3f} is below the baseline {b.first_contact_resolution:.3f}")
    if e.solved_rate < SOLVED_FLOOR:
        out.append(f"'solved?' {e.solved_rate:.3f} is below {SOLVED_FLOOR}")
    return out


class Ladder:
    def __init__(self, conn: sqlite3.Connection, clock, ledger=None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        conn.executescript(SCHEMA)

    def level(self, playbook_id: str) -> str:
        r = self.conn.execute("SELECT level FROM rollout_levels WHERE playbook_id = ?", (playbook_id,)).fetchone()
        return r["level"] if r else "L0"

    def levels(self) -> dict[str, dict]:
        return {r["playbook_id"]: {"level": r["level"], "reason": r["reason"], "updated_at": r["updated_at"]}
                for r in self.conn.execute("SELECT * FROM rollout_levels ORDER BY playbook_id")}

    def evaluate(self, playbook_id: str, e: Evidence, b: Baseline) -> Decision:
        cur = self.level(playbook_id)
        i = LEVELS.index(cur)
        fails = floor_failures(e, b)
        if e.forbidden_executed > 0:
            return Decision(playbook_id, "DEMOTE" if cur != "L0" else "HOLD", cur, "L0", fails)
        if fails:
            return Decision(playbook_id, "DEMOTE" if i > 0 else "HOLD", cur, LEVELS[max(0, i - 1)], fails)
        if e.cases < MIN_CASES:
            return Decision(playbook_id, "HOLD", cur, cur, [f"only {e.cases} cases at {cur} (need {MIN_CASES})"])
        if i == len(LEVELS) - 1:
            return Decision(playbook_id, "HOLD", cur, cur, ["already at the top level"])
        return Decision(playbook_id, "PROMOTE", cur, LEVELS[i + 1], ["every floor holds on enough cases"])

    def apply(self, d: Decision) -> Decision:
        """Write the change (and a ledger entry) when the level moves. Only the named playbook's row is touched."""
        if d.level_after == d.level_before:
            return d
        self.conn.execute("INSERT INTO rollout_levels(playbook_id, level, reason, updated_at) VALUES (?,?,?,?) "
                          "ON CONFLICT(playbook_id) DO UPDATE SET level = excluded.level, reason = excluded.reason, updated_at = excluded.updated_at",
                          (d.playbook_id, d.level_after, "; ".join(d.reasons)[:500], self.clock.now_iso()))
        if self.ledger is not None:
            self.ledger.append(kind="STATE", actor="SYSTEM", payload={"rollout": d.action, "playbook": d.playbook_id, "from": d.level_before, "to": d.level_after,
                                                                       "reasons": d.reasons})
        return d

    def step(self, playbook_id: str, e: Evidence, b: Baseline) -> Decision:
        return self.apply(self.evaluate(playbook_id, e, b))
