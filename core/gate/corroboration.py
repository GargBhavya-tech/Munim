"""Corroboration levels for a claim (Bible section 29). Categorical, computed by code, NO weighted score.

Evidence is a list of ``Evidence(kind, entailed, contradicted, forbids_rule)`` items, one per source checked against the claim:
kind "tool" = a system-of-record tool result, "kb" = an approved document chunk, "policy" = a policy rule.

Decision table, applied top to bottom (first match wins; the order IS the precedence):
  1. any item contradicted, or any item forbids_rule        -> CONTRADICTED  (rule_id = first forbidding rule, if any)
  2. a tool item entailed AND a kb/policy item entailed     -> CORROBORATED
  3. any item entailed                                      -> SUPPORTED
  4. otherwise (including no evidence at all)               -> UNSUPPORTED

"Consistent with an approved document or policy rule" is read as: a kb or policy source ENTAILS the claim, and (by rule 1) nothing
contradicts or forbids it. A kb/policy item that is merely silent (neither entailed nor contradicted) does not corroborate.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

KINDS = ("tool", "kb", "policy")


class Level(str, Enum):
    CORROBORATED = "CORROBORATED"
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"


# level -> (chip colour, double tick, may be sent)
CHIPS = {
    Level.CORROBORATED: ("green", True, True),
    Level.SUPPORTED: ("green", False, True),
    Level.UNSUPPORTED: ("red", False, False),
    Level.CONTRADICTED: ("red", False, False),
}


@dataclass(frozen=True)
class Evidence:
    kind: str                       # "tool" | "kb" | "policy"
    entailed: bool = False
    contradicted: bool = False
    forbids_rule: str | None = None  # policy rule id that forbids the claim

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"evidence kind must be one of {KINDS}, got {self.kind!r}")


@dataclass(frozen=True)
class LevelResult:
    level: Level
    rule_id: str | None
    chip: str            # "green double-tick" | "green" | "red"
    sendable: bool

    @property
    def color(self) -> str:
        return CHIPS[self.level][0]

    @property
    def double_tick(self) -> bool:
        return CHIPS[self.level][1]


def _result(level: Level, rule_id: str | None = None) -> LevelResult:
    color, tick, sendable = CHIPS[level]
    return LevelResult(level, rule_id, f"{color} double-tick" if tick else color, sendable)


def level(evidence: Iterable[Evidence]) -> LevelResult:
    items = list(evidence)
    if any(e.contradicted or e.forbids_rule for e in items):
        rule = next((e.forbids_rule for e in items if e.forbids_rule), None)
        return _result(Level.CONTRADICTED, rule)
    tool_ok = any(e.kind == "tool" and e.entailed for e in items)
    doc_ok = any(e.kind in ("kb", "policy") and e.entailed for e in items)
    if tool_ok and doc_ok:
        return _result(Level.CORROBORATED)
    if tool_ok or doc_ok:
        return _result(Level.SUPPORTED)
    return _result(Level.UNSUPPORTED)
