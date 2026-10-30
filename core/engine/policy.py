"""Embedded policy engine: plain Python rule functions behind one interface.

Why not cedarpy: it is not in requirements.txt (no new dependencies allowed), and our rules need live reads (offer band,
settlement status, consent, contact budget) that Cedar would need us to pre-fetch into a context anyway. The rule record
format (id, title, severity, applies_to, source, condition) is engine-agnostic, so a Cedar or OPA backend can replace
this file later without touching callers: they only see ``PolicyEngine.evaluate(intent, reader) -> PolicyDecision``.

Guarantees implemented here, independent of any rule:
* DEFAULT DENY: a write that is not registered, not in the step allowlist (AGENT), or not agent-callable (AGENT) is BLOCKed.
* No admin bypass: humans skip the allowlist, never the rules.
* Outcome precedence: any BLOCK -> BLOCK; else any ESCALATE -> ESCALATE; WARN never changes the outcome.
* Rule inputs: intent fields, ``intent.facts`` (engine-known case facts), live reads (Reader / DB), limits. Never model prose.
* policy_hash covers rule definitions + the source of their conditions + limits + playbook files.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass
from core.actions.types import ActionIntent, ContactBudgetLike, PolicyDecision, Reader, Violation

ENGINE_VERSION = "policy-engine/1"
DEFAULT_DENY_ID = "POLICY-DEFAULT-DENY"
_RANK = {"BLOCK": 0, "ESCALATE": 1, "WARN": 2}
WRITE_CLASSES = (RiskClass.R1, RiskClass.R2, RiskClass.R3)


@dataclass(frozen=True)
class Finding:
    """What a rule condition returns when it fires."""
    reason: str
    route: str | None = None            # human role that should look at it (ESCALATE)
    severity: str | None = None         # overrides the rule's nominal severity (rare)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str                       # BLOCK | ESCALATE | WARN
    source: str                         # document and section the rule comes from
    condition: Callable[["Ctx"], Finding | None]
    applies_to: dict = field(default_factory=dict)   # optional keys: actions, roles, principals (lists); empty = everything

    def matches(self, intent: ActionIntent) -> bool:
        a = self.applies_to
        if a.get("actions") and intent.action not in a["actions"]:
            return False
        if a.get("principals") and intent.principal.kind not in a["principals"]:
            return False
        if a.get("roles") and intent.principal.role not in a["roles"]:
            return False
        return True

    def definition(self) -> dict:
        try:
            src = inspect.getsource(self.condition)
        except (OSError, TypeError):
            src = getattr(self.condition, "__qualname__", repr(self.condition))
        return {"id": self.id, "title": self.title, "severity": self.severity, "source": self.source,
                "applies_to": {k: sorted(v) for k, v in sorted(self.applies_to.items())}, "condition": src}


class Ctx:
    """Everything a rule may look at. Deliberately has no access to model text outside intent.args."""

    def __init__(self, intent: ActionIntent, spec: ActionSpec | None, engine: "PolicyEngine", reader: Reader | None):
        self.intent, self.spec, self.engine, self._reader = intent, spec, engine, reader
        self.conn: sqlite3.Connection = engine.conn
        self.limits: dict = engine.limits
        self.registry: ActionRegistry = engine.registry
        self.budget: ContactBudgetLike | None = engine.contact_budget

    @property
    def args(self) -> dict:
        return self.intent.args

    @property
    def facts(self) -> dict:
        return self.intent.facts

    def read(self, name: str, args: dict) -> dict | None:
        """Live read through the Reader. None when there is no reader or the read failed (rules must then fail closed)."""
        if self._reader is None:
            return None
        try:
            body = self._reader(name, args)
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    def merchant_id(self) -> str | None:
        return self.args.get("merchant_id") or self.facts.get("merchant_id")


def load_limits(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def compute_policy_hash(rules: Iterable[Rule], limits: dict, playbook_dir: str | Path | None = None) -> str:
    h = hashlib.sha256()
    h.update(ENGINE_VERSION.encode())
    h.update(json.dumps([r.definition() for r in rules], sort_keys=True).encode())
    h.update(json.dumps(limits, sort_keys=True, default=str).encode())
    if playbook_dir is not None and Path(playbook_dir).is_dir():
        for p in sorted(Path(playbook_dir).rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(playbook_dir)).encode())
                h.update(p.read_bytes())
    return h.hexdigest()


class PolicyEngine:
    def __init__(self, conn: sqlite3.Connection, clock, registry: ActionRegistry, rules: Iterable[Rule], limits: dict,
                 playbook_dir: str | Path | None = None, reader: Reader | None = None,
                 contact_budget: ContactBudgetLike | None = None):
        self.conn, self.clock, self.registry = conn, clock, registry
        self.rules = tuple(rules)
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule id")
        self.limits = limits
        self.playbook_dir = playbook_dir
        self.reader = reader
        self.contact_budget = contact_budget
        self.policy_hash = compute_policy_hash(self.rules, limits, playbook_dir)

    # -- the interface ---------------------------------------------------------------------------------
    def evaluate(self, intent: ActionIntent, reader: Reader | None = None) -> PolicyDecision:
        spec = self.registry.get(intent.action) if self.registry.has(intent.action) else None
        ctx = Ctx(intent, spec, self, reader or self.reader)
        out: list[Violation] = []

        deny = self._default_deny(intent, spec)
        if deny:
            out.append(self._violation(intent, DEFAULT_DENY_ID, "Default deny", "BLOCK", deny, "Bible section 30; Build Map #29"))

        for rule in self.rules:
            if not rule.matches(intent):
                continue
            try:
                f = rule.condition(ctx)
            except Exception as e:  # a broken rule must never become an allow
                out.append(self._violation(intent, rule.id, rule.title, "BLOCK", f"rule failed to evaluate ({type(e).__name__}); denied", rule.source))
                continue
            if f is not None:
                out.append(self._violation(intent, rule.id, rule.title, f.severity or rule.severity, f.reason, rule.source, f.route))

        out.sort(key=lambda v: (_RANK[v.severity], v.rule_id))
        if any(v.severity == "BLOCK" for v in out):
            outcome = "BLOCK"
        elif any(v.severity == "ESCALATE" for v in out):
            outcome = "ESCALATE"
        else:
            outcome = "ALLOW"
        return PolicyDecision(outcome, tuple(out), self.policy_hash)

    # -- internals -------------------------------------------------------------------------------------
    def _default_deny(self, intent: ActionIntent, spec: ActionSpec | None) -> str | None:
        if spec is None:
            return f"{intent.action!r} is not a registered action"   # unknown / forbidden names are treated as writes
        if spec.cls not in WRITE_CLASSES:
            return None                                              # R0 and DRAFT are always allowed
        if intent.principal.kind == "AGENT":
            if not spec.agent_callable:
                return f"{intent.action!r} is not agent-callable"
            if intent.allowlist is None or intent.action not in intent.allowlist:
                return f"{intent.action!r} is not permitted by the current playbook step"
        return None

    def _violation(self, intent, rule_id, title, severity, reason, source, route=None) -> Violation:
        return Violation(rule_id=rule_id, title=title, severity=severity, reason=reason, source=source, action=intent.action,
                         case_id=intent.case_id, policy_hash=self.policy_hash, route=route)
