"""Style learning (#104; Bible section 12): which STYLE works for which segment, learned by Thompson sampling.

* What varies is style only: channel, format, language mix, time of day and opening line (an id of an approved fixed phrase). Facts, prices and
  disclosures never vary, and an option is a short label, never a sentence, so the bandit cannot carry a number to a merchant.
* One Beta(alpha, beta) posterior per (segment, dimension, option); every dimension is sampled independently, so five dimensions with a few options
  each are learned from far fewer contacts than the full grid of combinations would need. Segments never share evidence.
* THE REWARD IS A QUALIFIED CONVERSATION OR A CONVERSION, never a reply. ``record`` maps an outcome to 1 only for QUALIFIED / WON / CONVERTED;
  REPLIED, ENGAGED, NO_REPLY and the rest teach 0. A style that gets answers and no business therefore loses (the named mistake is optimising
  for reply rate). Late outcomes are fine: a decision waits, un-rewarded, until ``record`` is called for it.
* Explicit preferences ("English please", "call after 9 pm", saved to the CRM) override: the dimension takes the stated value, nothing is sampled
  for it, and that forced choice is NOT learned from (the person chose it, the bandit did not).
* Deterministic: the sampler is seeded from (seed, segment, decision number); no wall clock.
* Callers pass only options that are legal for the lead (e.g. channels it has consented to); the bandit never adds an option.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

import numpy as np

from core.engine.clock import SimClock
from core.memory.store import write_txn

DIMENSIONS = ("channel", "format", "language_mix", "time", "opening_line")
REWARD_OUTCOMES = frozenset({"QUALIFIED", "WON", "CONVERTED"})
PRIOR_ALPHA = PRIOR_BETA = 1.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS style_arms (
  segment TEXT NOT NULL, dimension TEXT NOT NULL, option TEXT NOT NULL, alpha REAL NOT NULL, beta REAL NOT NULL, pulls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (segment, dimension, option)
);
CREATE TABLE IF NOT EXISTS style_decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT, segment TEXT NOT NULL, subject_id TEXT, choice_json TEXT NOT NULL, learned_json TEXT NOT NULL,
  ts TEXT NOT NULL, outcome TEXT, reward INTEGER, rewarded_ts TEXT
)
"""


class BanditError(ValueError):
    pass


@dataclass(frozen=True)
class StyleChoice:
    decision_id: int
    segment: str
    choice: dict[str, str]                 # dimension -> option
    forced: tuple[str, ...]                # dimensions an explicit preference set


def reward_for(outcome: str) -> int:
    """1 for a qualified conversation or a conversion; 0 for everything else, replies included."""
    return 1 if outcome in REWARD_OUTCOMES else 0


class StyleBandit:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, seed: int = 0):
        self.conn, self.clock, self.seed = conn, clock, seed
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)

    # ------------------------------------------------------------------------------------------------------ posteriors
    def posterior(self, segment: str, dimension: str, option: str) -> tuple[float, float, int]:
        r = self.conn.execute("SELECT alpha, beta, pulls FROM style_arms WHERE segment = ? AND dimension = ? AND option = ?", (segment, dimension, option)).fetchone()
        return (r["alpha"], r["beta"], r["pulls"]) if r else (PRIOR_ALPHA, PRIOR_BETA, 0)

    def _rng(self, segment: str) -> np.random.Generator:
        n = self.conn.execute("SELECT COUNT(*) FROM style_decisions WHERE segment = ?", (segment,)).fetchone()[0]
        h = hashlib.sha256(f"{self.seed}|{segment}|{n}".encode("utf-8")).digest()
        return np.random.default_rng(int.from_bytes(h[:8], "big"))

    # ------------------------------------------------------------------------------------------------------ choosing
    def choose(self, segment: str, options: dict[str, list[str]], explicit: dict[str, str] | None = None, subject_id: str | None = None) -> StyleChoice:
        explicit = explicit or {}
        for dim in (*options, *explicit):
            if dim not in DIMENSIONS:
                raise BanditError(f"unknown style dimension {dim!r}")
        for dim, opts in options.items():
            if not opts:
                raise BanditError(f"no legal option for {dim!r}")
        rng = self._rng(segment)
        choice: dict[str, str] = {}
        forced: list[str] = []
        for dim in DIMENSIONS:
            if dim in explicit:
                choice[dim] = explicit[dim]
                forced.append(dim)
            elif dim in options:
                opts = sorted(set(options[dim]))
                draws = [float(rng.beta(*self.posterior(segment, dim, o)[:2])) for o in opts]
                choice[dim] = opts[int(np.argmax(draws))]
        learned = [d for d in choice if d not in forced]
        with write_txn(self.conn):
            cur = self.conn.execute("INSERT INTO style_decisions(segment, subject_id, choice_json, learned_json, ts) VALUES (?,?,?,?,?)",
                                    (segment, subject_id, json.dumps(choice, sort_keys=True), json.dumps(learned), self.clock.now_iso()))
        return StyleChoice(cur.lastrowid, segment, choice, tuple(forced))

    # ------------------------------------------------------------------------------------------------------ learning
    def record(self, decision_id: int, outcome: str) -> int:
        """Teach the bandit what became of a decision. Returns the reward (1 or 0). A decision can be rewarded once."""
        d = self.conn.execute("SELECT * FROM style_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        if d is None:
            raise KeyError(decision_id)
        if d["outcome"] is not None:
            return d["reward"]                                           # already learned from: never counted twice
        r = reward_for(outcome)
        choice, learned = json.loads(d["choice_json"]), json.loads(d["learned_json"])
        with write_txn(self.conn):
            for dim in learned:
                self.conn.execute("INSERT OR IGNORE INTO style_arms(segment, dimension, option, alpha, beta, pulls) VALUES (?,?,?,?,?,0)",
                                  (d["segment"], dim, choice[dim], PRIOR_ALPHA, PRIOR_BETA))
                self.conn.execute("UPDATE style_arms SET alpha = alpha + ?, beta = beta + ?, pulls = pulls + 1 WHERE segment = ? AND dimension = ? AND option = ?",
                                  (r, 1 - r, d["segment"], dim, choice[dim]))
            self.conn.execute("UPDATE style_decisions SET outcome = ?, reward = ?, rewarded_ts = ? WHERE decision_id = ?", (outcome, r, self.clock.now_iso(), decision_id))
        return r

    def best(self, segment: str, dimension: str) -> str | None:
        """The option with the highest posterior mean (reporting only; choosing always samples)."""
        rows = self.conn.execute("SELECT option, alpha, beta FROM style_arms WHERE segment = ? AND dimension = ?", (segment, dimension)).fetchall()
        return max(rows, key=lambda r: (r["alpha"] / (r["alpha"] + r["beta"]), r["option"]))["option"] if rows else None


__all__ = ["StyleBandit", "StyleChoice", "BanditError", "DIMENSIONS", "REWARD_OUTCOMES", "reward_for"]
