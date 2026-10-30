"""Metrics with intervals (#142). Every rate comes with a 95% Wilson interval; zeros get the rule of three; repeated seeded runs give pass^k.

Inputs are ``Observation`` rows from core.eval.simulator (final state + ledger), never transcripts. The frozen claim and probe sets
are READ-ONLY inputs: ``load_frozen`` re-hashes every file against MANIFEST.sha256 and refuses to return anything if one differs,
is missing, or an unlisted file appeared.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from math import comb
from pathlib import Path
from typing import Iterable, Mapping, Sequence

Z95 = 1.959964          # two-sided 95% normal quantile


# ------------------------------------------------------------------------------------------------ intervals
@dataclass(frozen=True)
class Rate:
    k: int
    n: int
    value: float | None
    lo: float
    hi: float
    method: str                      # "wilson" | "rule_of_three" | "none" (n == 0)

    def as_dict(self) -> dict:
        return {"k": self.k, "n": self.n, "value": self.value, "lo": round(self.lo, 4), "hi": round(self.hi, 4), "method": self.method}


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval. (0, 1) when n == 0 (nothing is known)."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n, got k={k} n={n}")
    if n == 0:
        return 0.0, 1.0
    p, z2 = k / n, z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(k: int, n: int) -> Rate:
    """k/n with a 95% interval. At k == 0 (or k == n) the one-sided rule of three (3/n) replaces the Wilson bound on the open side."""
    if n == 0:
        return Rate(0, 0, None, 0.0, 1.0, "none")
    if k == 0:
        return Rate(k, n, 0.0, 0.0, min(1.0, 3 / n), "rule_of_three")
    if k == n:
        return Rate(k, n, 1.0, max(0.0, 1 - 3 / n), 1.0, "rule_of_three")
    lo, hi = wilson(k, n)
    return Rate(k, n, k / n, lo, hi, "wilson")


# ------------------------------------------------------------------------------------------------ pass^k
def pass_hat_k(successes: int, trials: int, k: int) -> float:
    """Probability that k trials drawn without replacement from this scenario's `trials` runs ALL pass: C(c,k)/C(n,k) (tau-bench)."""
    if not 1 <= k <= trials or not 0 <= successes <= trials:
        raise ValueError(f"need 1 <= k <= trials and 0 <= successes <= trials, got c={successes} n={trials} k={k}")
    return comb(successes, k) / comb(trials, k)


@dataclass(frozen=True)
class PassK:
    k: int
    scenarios: int
    value: float | None
    all_k_passed: Rate               # scenarios where every one of the k runs passed (exact when trials == k), with its Wilson interval


def pass_k(outcomes: Mapping[str, Sequence[bool]], k: int) -> PassK:
    """outcomes: scenario id -> pass/fail of each repeated seeded run. Every scenario needs >= k runs."""
    if not outcomes:
        return PassK(k, 0, None, rate(0, 0))
    vals = [pass_hat_k(sum(map(bool, r)), len(r), k) for r in outcomes.values()]
    full = sum(1 for r in outcomes.values() if sum(map(bool, r)) == len(r) and len(r) >= k)
    return PassK(k, len(vals), sum(vals) / len(vals), rate(full, len(vals)))


# ------------------------------------------------------------------------------------------------ frozen sets
class FrozenSetTampered(RuntimeError):
    pass


def _listed(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts and p.suffix != ".pyc")


def verify_manifest(d: Path) -> None:
    """Raise FrozenSetTampered unless every file in `d` matches MANIFEST.sha256 exactly (no change, no missing, no extra file)."""
    d = Path(d)
    mf = d / "MANIFEST.sha256"
    if not mf.is_file():
        raise FrozenSetTampered(f"{d}: MANIFEST.sha256 missing")
    want: dict[str, str] = {}
    for line in mf.read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, _, rel = line.partition("  ")
            want[rel.strip()] = h.strip()
    have = {p.relative_to(d).as_posix(): p for p in _listed(d)}
    bad = [f"missing {r}" for r in want if r not in have] + [f"unlisted {r}" for r in have if r not in want]
    bad += [f"changed {r}" for r, p in have.items() if r in want and hashlib.sha256(p.read_bytes()).hexdigest() != want[r]]
    if bad:
        raise FrozenSetTampered(f"{d}: frozen set does not match its manifest: " + "; ".join(bad))


FROZEN_ROOT = Path(__file__).resolve().parent.parent.parent / "eval" / "frozen"


def load_frozen(name: str, root: Path | None = None) -> dict:
    """Verified, read-only load of eval/frozen/<claims|probes>. Raises FrozenSetTampered before returning anything if the manifest fails."""
    d = (root or FROZEN_ROOT) / name
    verify_manifest(d)
    if name == "claims":
        return {"claims": [json.loads(x) for x in (d / "claims.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]}
    if name == "probes":
        return {"policy": json.loads((d / "policy_probes.json").read_text(encoding="utf-8")),
                "escalation": json.loads((d / "escalation_snapshots.json").read_text(encoding="utf-8"))}
    raise ValueError(f"unknown frozen set {name!r}")


# ------------------------------------------------------------------------------------------------ confusion-style metrics
def precision_recall(pairs: Iterable[tuple[bool, bool]]) -> dict[str, Rate]:
    """pairs: (should, did). precision = did&should / did; recall = did&should / should. Each with an interval."""
    tp = fp = fn = 0
    for should, did in pairs:
        tp += should and did
        fp += (not should) and did
        fn += should and not did
    return {"precision": rate(tp, tp + fp), "recall": rate(tp, tp + fn)}


def _per_playbook(obs: Sequence, pred) -> dict[str, Rate]:
    out: dict[str, list[int]] = {}
    for o in obs:
        c = out.setdefault(o.playbook_id, [0, 0])
        c[0] += bool(pred(o))
        c[1] += 1
    return {pb: rate(*c) for pb, c in sorted(out.items())}


def report(obs: Sequence, *, k: int = 4, policy_pairs: Iterable[tuple[bool, bool]] | None = None,
           grounding: Iterable[tuple[bool, bool]] | None = None, cost: Mapping[str, float] | None = None) -> dict:
    """All tiers from observations. `policy_pairs` = (should_refuse, did_refuse); `grounding` = (claim_is_bad, was_blocked); `cost` is passed through."""
    obs = list(obs)
    n = len(obs)
    by_sc: dict[str, list[bool]] = {}
    for o in obs:
        by_sc.setdefault(o.scenario_id, []).append(o.passed)
    runs_ok = all(len(v) >= k for v in by_sc.values()) and bool(by_sc)
    tier_a = {
        "wrong_action_rate": rate(sum(o.wrong_action for o in obs), n),
        "forbidden_executed": rate(sum(o.forbidden_executed > 0 for o in obs), n),
        "unsupported_claim_rate": rate(sum(o.unsupported_sent for o in obs), sum(o.claims_sent for o in obs)),
        "escalation": precision_recall((o.expected_escalation, o.escalated) for o in obs),
        "ledger_chain_intact": rate(sum(o.ledger_ok for o in obs), n),
    }
    if policy_pairs is not None:
        tier_a["policy"] = precision_recall(policy_pairs)
    if grounding is not None:
        g = list(grounding)
        tier_a["grounding_catch"] = rate(sum(1 for bad, blocked in g if bad and blocked), sum(1 for bad, _ in g if bad))
        tier_a["grounding_false_block"] = rate(sum(1 for bad, blocked in g if not bad and blocked), sum(1 for bad, _ in g if not bad))
    tier_b = {
        "resolution_rate": rate(sum(o.resolved for o in obs), n),
        "resolution_by_playbook": _per_playbook(obs, lambda o: o.resolved),
        "pass_k": pass_k(by_sc, k) if runs_ok else None,
        "mean_turns": (sum(o.turns for o in obs) / n) if n else None,
        "mean_touches": (sum(o.touches for o in obs) / n) if n else None,
    }
    return {"tier_a": tier_a, "tier_b": tier_b, "tier_c": dict(cost or {}), "runs": n}
