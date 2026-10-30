"""Playbook schema and loader (Bible §15).

A playbook cannot grant a power the pack does not define: the loader rejects unknown actions, forbidden
actions, unknown KB scopes, unknown done-check fields and any expression outside the safe language.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from core.actions.registry import FORBIDDEN_ACTIONS, ActionRegistry, RiskClass
from core.engine import fields as F
from core.engine.safe_expr import Expr, ExpressionError as _ExprErr, UnknownFieldError as _UnkField, compile_expr


class PlaybookError(Exception):
    code = "PLAYBOOK_INVALID"

    def __init__(self, message: str, *, playbook: str | None = None):
        super().__init__(f"{playbook + ': ' if playbook else ''}{message}")
        self.playbook = playbook


class SchemaError(PlaybookError):
    code = "SCHEMA"


class ForbiddenActionError(PlaybookError):
    code = "FORBIDDEN_ACTION"


class UnknownActionError(PlaybookError):
    code = "UNKNOWN_ACTION"


class UnknownKBScopeError(PlaybookError):
    code = "UNKNOWN_KB_SCOPE"


class UnknownFieldError(PlaybookError):
    code = "UNKNOWN_FIELD"


class ExpressionError(PlaybookError):
    code = "BAD_EXPRESSION"


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    done: str | None = None     # safe expression; defaults to "steps.<name>.done"

    @model_validator(mode="after")
    def _default_done(self):
        if not self.done:
            self.done = f"steps.{self.name}.done"
        return self


class AllowedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    max_per_case: int | None = None
    precondition: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)   # any other key, e.g. {type: DEVICE}


class Triggers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    half: Literal["sales", "service"]
    priority_lane: Literal["P0", "P1", "P2", "P3"]
    triggers: Triggers = Field(default_factory=Triggers)
    required_reads: list[str] = Field(default_factory=list)
    steps: list[Step]
    allowed_actions: list[AllowedAction] = Field(default_factory=list)
    done_check: str
    alt_terminal: list[str] | None = None
    escalate_when: list[str] = Field(default_factory=list)
    kb_scope: list[str] = Field(default_factory=list)
    max_touches: int = Field(gt=0)
    sla_sim_hours: int = Field(gt=0)

    _exprs: dict[str, Expr] = PrivateAttr(default_factory=dict)

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, v):
        if not isinstance(v, list):
            raise ValueError("steps must be a list")
        return [{"name": s} if isinstance(s, str) else s for s in v]

    @field_validator("allowed_actions", mode="before")
    @classmethod
    def _actions(cls, v):
        if not isinstance(v, list):
            raise ValueError("allowed_actions must be a list")
        out = []
        for a in v:
            if isinstance(a, str):
                out.append({"name": a})
            elif isinstance(a, dict) and "name" in a:
                core = {k: a[k] for k in ("name", "max_per_case", "precondition") if k in a}
                rest = {k: x for k, x in a.items() if k not in core}
                out.append({**core, "constraints": rest} if rest else core)
            elif isinstance(a, dict) and len(a) == 1:
                (name, spec), = a.items()
                spec = dict(spec or {})
                core = {k: spec.pop(k) for k in ("max_per_case", "precondition") if k in spec}
                out.append({"name": name, **core, **({"constraints": spec} if spec else {})})
            else:
                raise ValueError(f"cannot read allowed action {a!r}")
        return out

    # -- helpers -----------------------------------------------------------------------------------
    @property
    def fields_allowed(self) -> set[str]:
        return F.allowed_fields([s.name for s in self.steps])

    def expr(self, src: str) -> Expr:
        """Compiled expression for ``src`` (compiling is pure; reads are never cached)."""
        e = self._exprs.get(src)
        if e is None:
            e = self._exprs[src] = compile_expr(src, self.fields_allowed)
        return e

    def action(self, name: str) -> AllowedAction | None:
        return next((a for a in self.allowed_actions if a.name == name), None)

    def all_expressions(self) -> list[tuple[str, str]]:
        out = [("done_check", self.done_check)]
        out += [(f"steps.{s.name}.done", s.done) for s in self.steps]
        out += [(f"allowed_actions.{a.name}.precondition", a.precondition) for a in self.allowed_actions if a.precondition]
        out += [(f"escalate_when[{i}]", e) for i, e in enumerate(self.escalate_when)]
        return out


# -- loader -------------------------------------------------------------------------------------------
def kb_scopes_from_dir(kb_dir: str | Path) -> set[str]:
    """The ``scopes`` values found in the front matter of every KB document in the directory."""
    scopes: set[str] = set()
    for p in sorted(Path(kb_dir).glob("*.md")):
        text = p.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        meta = yaml.safe_load(text[3:end]) or {}
        scopes.update(str(s) for s in meta.get("scopes", []) or [])
    return scopes


def _check_action(name: str, registry: ActionRegistry, pid: str, *, read: bool = False) -> None:
    if name in FORBIDDEN_ACTIONS:
        raise ForbiddenActionError(f"action {name!r} is forbidden (FORBIDDEN_ACTION)", playbook=pid)
    if not registry.has(name):
        raise UnknownActionError(f"action {name!r} is not in the pack's action registry", playbook=pid)
    spec = registry.get(name)
    if read and spec.cls is not RiskClass.R0:
        raise UnknownActionError(f"required read {name!r} is not an R0 read", playbook=pid)
    if not read and not spec.agent_callable:
        raise UnknownActionError(f"action {name!r} is not callable by the agent", playbook=pid)


def _compile_all(pb: Playbook) -> None:
    for where, src in pb.all_expressions():
        try:
            e = pb.expr(src)
        except _UnkField as ex:
            raise UnknownFieldError(f"{where}: unknown field {ex.field!r}", playbook=pb.id) from None
        except _ExprErr as ex:
            raise ExpressionError(f"{where}: {ex}", playbook=pb.id) from None
        for f in e.fields:
            need = F.read_for_field(f)
            if need and need not in pb.required_reads:
                raise UnknownFieldError(f"{where}: field {f!r} needs the read {need!r} in required_reads", playbook=pb.id)


def parse_playbook(data: Any, registry: ActionRegistry, kb_scopes: set[str] | None = None, *, source: str = "") -> Playbook:
    if not isinstance(data, dict):
        raise SchemaError(f"{source or 'playbook'} must be a YAML mapping")
    pid = str(data.get("id") or source or "?")
    # forbidden / unknown names are reported before schema detail so the cause is unmistakable
    for a in data.get("allowed_actions") or []:
        name = a if isinstance(a, str) else (a.get("name") if isinstance(a, dict) and "name" in a else next(iter(a), None) if isinstance(a, dict) else None)
        if isinstance(name, str):
            _check_action(name, registry, pid)
    try:
        pb = Playbook.model_validate(data)
    except ValidationError as e:
        raise SchemaError(str(e), playbook=pid) from None
    for a in pb.allowed_actions:
        _check_action(a.name, registry, pb.id)
    for r in pb.required_reads:
        _check_action(r, registry, pb.id, read=True)
    names = [s.name for s in pb.steps]
    if len(set(names)) != len(names) or not names:
        raise SchemaError("step names must be unique and non-empty", playbook=pb.id)
    if kb_scopes is not None:
        for s in pb.kb_scope:
            if s not in kb_scopes:
                raise UnknownKBScopeError(f"kb_scope {s!r} is not a known knowledge-base scope", playbook=pb.id)
    _compile_all(pb)
    return pb


def load_playbook(path: str | Path, registry: ActionRegistry, kb_scopes: set[str] | None = None) -> Playbook:
    """Load and validate one playbook. With ``kb_scopes=None`` the scopes are read from the pack's ``kb``
    directory next to the playbooks directory (skipped if there is none)."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SchemaError(f"{path.name}: invalid YAML: {e}") from None
    if kb_scopes is None:
        kb_dir = path.parent.parent / "kb"
        kb_scopes = kb_scopes_from_dir(kb_dir) if kb_dir.is_dir() else None
    return parse_playbook(data, registry, kb_scopes, source=path.stem)


def load_playbooks(dir: str | Path, registry: ActionRegistry, kb_scopes: set[str] | None = None) -> dict[str, Playbook]:
    out: dict[str, Playbook] = {}
    for p in sorted(Path(dir).glob("*.yaml")):
        pb = load_playbook(p, registry, kb_scopes)
        if pb.id in out:
            raise SchemaError(f"duplicate playbook id {pb.id!r}", playbook=pb.id)
        out[pb.id] = pb
    return out
