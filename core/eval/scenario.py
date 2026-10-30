"""Scenario files: one persona, one hidden goal, a script of world events, and the expected FINAL STATE.

Success is judged by comparing the final state of the mock systems with ``expected`` (the tau-bench method), never by
an LLM reading a transcript. Files live in packs/<pack>/scenarios/{dev,heldout}/<playbook>__<name>.yaml.
Held-out scenarios are written WITHOUT looking at failures of the dev ones (the independence rule, Bible section 38).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Family = Literal["happy", "multi_case", "should_escalate", "adversarial", "noise"]
CaseState = Literal["RESOLVED", "ESCALATED", "PARKED", "DECLINED", "WAITING", "BLOCKED", "NOT_NOW"]


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merchant_id: str                       # a seeded merchant (M-0001 .. M-0200); never real data
    language: str                          # hi-IN | kn-IN | en-IN | hinglish
    traits: list[str] = Field(default_factory=list)   # e.g. ["angry", "confused", "injecting", "wants-a-human"]
    hidden_goal: str = ""                  # what the simulated merchant wants, in one sentence


class Step(BaseModel):
    """One thing that happens in the world. Exactly one of the action fields is set."""
    model_config = ConfigDict(extra="forbid")
    after_hours: float = 0.0               # sim hours to advance BEFORE this step happens
    event: str | None = None               # an event type from core.engine.events.EVENT_TYPES
    merchant_says: str | None = None       # a merchant message (text, in the persona's language)
    control: dict[str, Any] | None = None  # {"path": <a scenario-control path containing {mid}>, "body": {...}}; {mid} is substituted
    pay: int | None = None                 # customer pays this many paise
    advance_settlement: str | None = None  # SETTLED | HELD_RISK | FAILED_BANK: advance the merchant's latest pending settlement
    chaos: dict[str, Any] | None = None    # {"system": "*", "mode": "...", "rate": 0.3}

    @model_validator(mode="after")
    def _one_action(self):
        n = sum(x is not None for x in (self.event, self.merchant_says, self.control, self.pay, self.advance_settlement, self.chaos))
        if n != 1:
            raise ValueError("a step must set exactly one of event, merchant_says, control, pay, advance_settlement, chaos")
        return self


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected: bool
    reason_class: str | None = None        # e.g. RISK_HOLD, OUT_OF_BAND, FRAUD, REGULATORY, WANTS_HUMAN, REPEAT_FAULT


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_state: CaseState
    done_fields: dict[str, Any] = Field(default_factory=dict)       # dotted fields from core.engine.fields, e.g. {"kyc.status": "APPROVED"}
    escalation: Escalation
    forbidden_actions: list[str] = Field(default_factory=list)     # actions that must NEVER execute in this scenario
    required_actions: list[str] = Field(default_factory=list)      # actions that must have executed exactly once
    max_touches: int = 4


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    playbook_id: str
    family: Family
    description: str
    persona: Persona
    steps: list[Step] = Field(min_length=1)
    expected: Expected
    split: Literal["dev", "heldout"] | None = None   # filled from the directory


def load_scenario(path: Path) -> Scenario:
    sc = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    split = path.parent.name
    if split not in ("dev", "heldout"):
        raise ValueError(f"{path}: scenarios must live under scenarios/dev or scenarios/heldout")
    if not path.name.startswith(f"{sc.playbook_id}__"):
        raise ValueError(f"{path.name}: file name must start with '{sc.playbook_id}__'")
    sc.split = split
    return sc


def load_scenarios(scenarios_dir: Path) -> list[Scenario]:
    out = [load_scenario(p) for split in ("dev", "heldout") for p in sorted((scenarios_dir / split).glob("*.yaml"))]
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario ids")
    return out


def coverage(scenarios: list[Scenario]) -> dict[str, dict[str, int]]:
    """Per playbook: how many dev / held-out scenarios and which families. The 'supported' bar needs >=6 dev, >=3 held-out."""
    out: dict[str, dict] = {}
    for s in scenarios:
        c = out.setdefault(s.playbook_id, {"dev": 0, "heldout": 0, "families": set()})
        c[s.split] += 1
        c["families"].add(s.family)
    return {k: {"dev": v["dev"], "heldout": v["heldout"], "families": sorted(v["families"])} for k, v in out.items()}


def meets_supported_bar(c: dict) -> bool:
    return c["dev"] >= 6 and c["heldout"] >= 3
