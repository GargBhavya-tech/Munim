"""Guard ablations (#143): the table that shows each guard is what stops the harm.

A row is one guard. ``on`` is what happened with the guard in place (harm must be False), ``off`` is the same scenario with ONLY that guard
removed (harm must be True). A guard whose removal changes nothing proves nothing, so ``passes`` needs both. The "off" arrangements live in the
tests (tests/test_ablations.py); no production code path has a switch for them.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Ablation:
    guard: str
    scenario: str
    on_outcome: str
    on_harm: bool
    off_outcome: str
    off_harm: bool

    @property
    def passes(self) -> bool:
        return (not self.on_harm) and self.off_harm

    def as_dict(self) -> dict:
        return {**asdict(self), "passes": self.passes}


def render_table(rows: list[Ablation]) -> str:
    head = f"{'guard':26s} {'guard ON':34s} {'guard OFF':34s} result"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r.guard:26s} {r.on_outcome[:32]:34s} {r.off_outcome[:32]:34s} {'PASS' if r.passes else 'FAIL'}")
    lines.append(f"{sum(r.passes for r in rows)} / {len(rows)} guards proven by removal")
    return "\n".join(lines)


def write_report(rows: list[Ablation], path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"label": "SIMULATED", "ablations": [r.as_dict() for r in rows],
                             "all_pass": all(r.passes for r in rows)}, indent=1, sort_keys=True), encoding="utf-8")
    return p
