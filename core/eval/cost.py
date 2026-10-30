"""Tier C: cost per resolved case in integer paise. Pure arithmetic over measured counts; no clock, no I/O except write_report.

Every rate below is an ASSUMPTION for modelling, never a real Paytm or vendor figure. Replace with the spike's price sheet.
Rates are in micro-paise (1e-6 paise) per unit so the whole computation stays in integers; rounding happens once, at the end.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.eval.latency import OUT_PATH, stage_report

MICRO = 1_000_000

# ASSUMPTION: illustrative rates, micro-paise per unit.
RATES_MICROPAISE = {
    "tokens_in": 20_000,       # 0.02 paise / token
    "tokens_out": 60_000,
    "stt_seconds": 8_000_000,  # 8 paise / second of audio
    "tts_chars": 300_000,
    "translate_chars": 200_000,
}
# ASSUMPTION (Bible Human baseline): minutes per case and loaded paise per minute for low/mid/high.
HUMAN_BASELINE = {"low": (6, 400), "mid": (10, 600), "high": (15, 1000)}
HUMAN_COST_PAISE = {k: m * c for k, (m, c) in HUMAN_BASELINE.items()}     # 2400 / 6000 / 15000 paise
ESCALATION_HUMAN_MINUTES = 10                                              # ASSUMPTION
ESCALATION_PAISE_PER_MIN = 600                                             # ASSUMPTION


@dataclass(frozen=True)
class CaseUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    stt_seconds: int = 0
    tts_chars: int = 0
    translate_chars: int = 0
    resolved: bool = True
    escalated: bool = False


def _round_half_up(micro: int) -> int:
    return (micro + MICRO // 2) // MICRO


def component_micro(u: CaseUsage, rates: dict | None = None) -> dict:
    r = rates or RATES_MICROPAISE
    return {k: getattr(u, k) * r[k] for k in r}


def case_cost_paise(u: CaseUsage, rates: dict | None = None) -> int:
    """AI cost of one case in integer paise, plus the human time spent if it escalated."""
    total = sum(component_micro(u, rates).values())
    if u.escalated:
        total += ESCALATION_HUMAN_MINUTES * ESCALATION_PAISE_PER_MIN * MICRO
    return _round_half_up(total)


def cost_report(cases: list[CaseUsage], rates: dict | None = None) -> dict:
    resolved = [c for c in cases if c.resolved]
    total_micro = 0
    by_component = {k: 0 for k in (rates or RATES_MICROPAISE)}
    esc_micro = 0
    for c in cases:
        for k, v in component_micro(c, rates).items():
            by_component[k] += v
            total_micro += v
        if c.escalated:
            esc_micro += ESCALATION_HUMAN_MINUTES * ESCALATION_PAISE_PER_MIN * MICRO
    total_micro += esc_micro
    per_resolved = None
    if resolved:                               # all spend (incl. failed/escalated cases) divided by resolved cases
        per_resolved = (total_micro + len(resolved) * MICRO // 2) // (len(resolved) * MICRO)
    return {
        "label": "SIMULATED / ASSUMPTION rates, not Paytm or vendor figures",
        "cases": len(cases), "resolved": len(resolved), "escalated": sum(c.escalated for c in cases),
        "total_paise": _round_half_up(total_micro),
        "cost_per_resolved_case_paise": per_resolved,
        "components_paise": {k: _round_half_up(v) for k, v in by_component.items()},
        "escalation_human_paise": _round_half_up(esc_micro),
        "human_baseline_paise": dict(HUMAN_COST_PAISE),
        "rates_micropaise": dict(rates or RATES_MICROPAISE),
    }


def write_report(turns: list[dict], cases: list[CaseUsage], path: Path | str = OUT_PATH) -> dict:
    report = {"latency_ms": stage_report(turns), "tier_c_cost": cost_report(cases)}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
