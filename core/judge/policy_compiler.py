"""Plain-language policy compiler (#134). An operator types a rule in words; it can only ever TIGHTEN policy.

    text --(deterministic templates, or an optional injected phraser that only PROPOSES a DSL dict)--> DSL dict
         --(pydantic schema, extra=forbid, no code anywhere)--> validated DSL --(tightening checks against the registry and limits)-->
         Rule (closure built here from the DSL, never from text) --(dry-run: every probe, with and without the rule)--> Draft
         --(ops_admin approves the exact rule_hash the operator saw)--> HUMAN_ACTION ledger entry --> engine rules re-synced.

Why it cannot relax anything:
* There is no template that permits, widens, raises or removes. A request containing such a word is refused before any parser or LLM sees it.
* Compiled rules only ADD findings. The engine takes the strictest outcome, so adding a rule cannot turn a BLOCK/ESCALATE into an ALLOW.
* The dry run re-proves that on probes (recent ledger intents + the frozen probe set): any probe whose outcome gets weaker fails the draft.
* Default-deny, the registry and the forbidden-action list are untouched. A forbidden action is not blockable here (it does not exist).
* The LLM path returns a dict. It goes through the same schema and the same checks, so a model cannot smuggle anything the templates cannot say.

State lives in the ledger only (ops POLICY_RULE_APPROVED / POLICY_RULE_REVERTED). Active rules = approved and not reverted since.
Revert removes only a compiled CUSTOM-* rule, which restores the baseline; it can never remove a pack rule.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Optional, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.actions.registry import FORBIDDEN_ACTIONS, ActionRegistry, RiskClass
from core.actions.types import ActionIntent, Principal
from core.audit.ledger import Ledger, canonical_json
from core.engine import roles as roles_mod
from core.engine.policy import Ctx, Finding, PolicyEngine, Rule, compute_policy_hash

RULE_PREFIX = "CUSTOM-"
OP_APPROVED, OP_REVERTED = "POLICY_RULE_APPROVED", "POLICY_RULE_REVERTED"
APPROVER_ROLE = "ops_admin"
SOURCE_DOC = "Bible section 30 (compiled tightening rule, #134)"
_OUTCOME_RANK = {"ALLOW": 0, "ESCALATE": 1, "BLOCK": 2}
WRITE = (RiskClass.R1, RiskClass.R2, RiskClass.R3)


class PolicyCompileError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message

    def as_json(self) -> dict:
        return {"error": self.code, "message": self.message}


# -- the DSL (the only thing that can become a rule) ----------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BlockAction(_Strict):
    template: Literal["block_action"]
    action: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    role: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z_]{0,31}$")


class RequireApprovalAbove(_Strict):
    template: Literal["require_approval_above"]
    amount_paise: int = Field(ge=1, le=10**12)
    comparison: Literal["gt", "gte"]
    action: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class LowerWaiverBand(_Strict):
    template: Literal["lower_waiver_band"]
    max_months: int = Field(ge=0, le=36)


Dsl = Annotated[Union[BlockAction, RequireApprovalAbove, LowerWaiverBand], Field(discriminator="template")]


class _DslBox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dsl: Dsl


def load_dsl(obj) -> BaseModel:
    """Schema check only. Anything outside the three templates (python source, an allow template, extra keys) fails to load."""
    if not isinstance(obj, dict):
        raise PolicyCompileError("INVALID_DSL", "a rule is a JSON object, not text or code")
    try:
        return _DslBox.model_validate({"dsl": obj}).dsl
    except ValidationError as e:
        first = e.errors()[0]
        raise PolicyCompileError("INVALID_DSL", f"{'.'.join(str(x) for x in first['loc'][1:]) or 'rule'}: {first['msg']}")


def rule_hash(dsl: BaseModel) -> str:
    return hashlib.sha256(canonical_json(dsl.model_dump(exclude_none=True)).encode("utf-8")).hexdigest()


def rule_id_for(dsl: BaseModel) -> str:
    return RULE_PREFIX + rule_hash(dsl)[:8].upper()


# -- text -> DSL (deterministic templates) ---------------------------------------------------------------
RELAXING = re.compile(
    r"\b(allow|allows|allowed|permit|permits|enable|grant|let|relax|raise|increase|extend|widen|loosen|remove|delete|drop|disable|"
    r"bypass|unblock|skip|override|exempt|whitelist|waive|lift|ignore|revoke\s+rule|no\s+longer|stop\s+blocking|turn\s+off)\b",
    re.I)
_NEGATED_ALLOW = re.compile(r"\b(?:do\s+not|don't|dont|never|must\s+not|cannot|can't|should\s+not|shouldn't)\s+(?:allow|permit)\b", re.I)
_ACTION = r"[a-z][a-z0-9_]*"
_ROLE = r"[a-z][a-z_ ]*"
_MONEY = r"(?:(?:rs\.?|inr|₹)\s*[\d,]+(?:\.\d{1,2})?(?:\s*(?:lakh|lac|crore))?|[\d,]+(?:\.\d{1,2})?\s*(?:rupees|rs|lakh|lac|crore|paise))"
_BLOCK_VERB = r"(?:block|forbid|deny|disallow|prohibit|ban)"
_TIGHTEN_VERB = r"(?:lower|reduce|cut|tighten|decrease)"
_BLOCK_FOR = re.compile(rf"^(?:please\s+)?{_BLOCK_VERB}\s+(?P<action>{_ACTION})(?:\s+(?:for|by|from)\s+(?:the\s+)?(?:role\s+)?(?P<role>{_ROLE}))?$")
_STOP_ROLE = re.compile(rf"^(?:please\s+)?stop\s+(?:the\s+)?(?:role\s+)?(?P<role>{_ROLE})\s+from\s+(?:doing\s+)?(?P<action>{_ACTION})$")
_APPROVAL = re.compile(rf"^(?:please\s+)?(?:require|escalate|route)\s+(?P<pre>[a-z_ ]*?)\s*(?P<cmp>above|over|at\s+or\s+above)\s+(?P<money>{_MONEY})"
                       rf"(?:\s+for\s+(?P<action>{_ACTION}))?$")
_LOWER_AMOUNT = re.compile(rf"^(?:please\s+)?{_TIGHTEN_VERB}\s+(?:the\s+)?(?:approval|amount|money)\s+(?:band|limit|threshold)\s+to\s+(?P<money>{_MONEY})"
                           rf"(?:\s+for\s+(?P<action>{_ACTION}))?$")
_LOWER_WAIVER = re.compile(rf"^(?:please\s+)?{_TIGHTEN_VERB}\s+(?:the\s+)?(?:fee\s+)?waiver\s+(?:band|limit|cap)\s+to\s+(?P<n>\d+)\s+months?$")
_APPROVAL_PRE = {"", "approval", "human approval", "ops approval", "approval for amounts", "approval for payments", "payments", "amounts",
                 "approval for money", "money", "approval for actions", "actions"}


def parse_money_paise(s: str) -> int:
    """'Rs 5,000' / '5000 rupees' / '₹2,500.50' / '2 lakh' / '700 paise' -> integer paise. Code parses the typed constant; nothing is estimated."""
    t = s.lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        raise PolicyCompileError("NO_TEMPLATE", f"no amount in {s!r}")
    try:
        n = Decimal(m.group(1))
    except InvalidOperation:
        raise PolicyCompileError("NO_TEMPLATE", f"bad amount {s!r}")
    if "paise" in t:
        paise = n
    else:
        mult = 100
        if "crore" in t:
            mult = 100 * 10_000_000
        elif "lakh" in t or "lac" in t:
            mult = 100 * 100_000
        paise = n * mult
    if paise != paise.to_integral_value():
        raise PolicyCompileError("NO_TEMPLATE", f"{s!r} is not a whole number of paise")
    return int(paise)


def _norm(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip().lower()).rstrip(" .!")
    return _NEGATED_ALLOW.sub("disallow", t)


def reject_relaxing(text: str) -> None:
    """Called BEFORE any parser or phraser. A request that asks to permit, raise, widen or remove is refused outright."""
    t = _norm(text)
    m = RELAXING.search(t)
    if m:
        raise PolicyCompileError("RELAXING_REQUEST", f"{m.group(0)!r}: the compiler only tightens policy. It cannot permit, raise, widen or remove a rule")


def parse_text(text: str) -> dict:
    """Plain language -> DSL dict, by templates only. Raises NO_TEMPLATE when no template matches the whole sentence."""
    reject_relaxing(text)
    t = _norm(text)
    if m := _BLOCK_FOR.match(t):
        d = {"template": "block_action", "action": m["action"]}
        if m["role"]:
            d["role"] = m["role"].strip().replace(" ", "_")
        return d
    if m := _STOP_ROLE.match(t):
        return {"template": "block_action", "action": m["action"], "role": m["role"].strip().replace(" ", "_")}
    if m := _APPROVAL.match(t):
        if m["pre"].strip() not in _APPROVAL_PRE:
            raise PolicyCompileError("NO_TEMPLATE", f"do not understand {m['pre'].strip()!r}")
        d = {"template": "require_approval_above", "amount_paise": parse_money_paise(m["money"]),
             "comparison": "gte" if m["cmp"].startswith("at") else "gt"}
        if m["action"]:
            d["action"] = m["action"]
        return d
    if m := _LOWER_AMOUNT.match(t):
        d = {"template": "require_approval_above", "amount_paise": parse_money_paise(m["money"]), "comparison": "gte"}
        if m["action"]:
            d["action"] = m["action"]
        return d
    if m := _LOWER_WAIVER.match(t):
        return {"template": "lower_waiver_band", "max_months": int(m["n"])}
    raise PolicyCompileError("NO_TEMPLATE", "no rule template matches. Supported: 'block <action> [for <role>]', "
                                            "'require approval above <amount> [for <action>]', 'lower the waiver band to <N> months', "
                                            "'lower the approval limit to <amount>'")


class Phraser(Protocol):
    """Optional LLM-assisted path. It may only PROPOSE a DSL dict; code validates it exactly as it validates parsed text."""
    def propose(self, text: str, *, actions: list[str], roles: list[str]) -> Optional[dict]: ...


# -- validation: schema is not enough, the rule must tighten ---------------------------------------------
def check_tightens(dsl: BaseModel, registry: ActionRegistry, limits: dict) -> None:
    def need_write(action: str):
        if action in FORBIDDEN_ACTIONS:
            raise PolicyCompileError("FORBIDDEN_ACTION", f"{action!r} is forbidden and does not exist in the registry; there is nothing to block")
        if not registry.has(action):
            raise PolicyCompileError("UNKNOWN_ACTION", f"{action!r} is not a registered action")
        if registry.get(action).cls not in WRITE:
            raise PolicyCompileError("NOT_A_WRITE", f"{action!r} is a read or a draft; only write actions can be restricted")

    if isinstance(dsl, BlockAction):
        need_write(dsl.action)
        if dsl.role is not None and dsl.role not in roles_mod.ROLES:
            raise PolicyCompileError("UNKNOWN_ROLE", f"{dsl.role!r} is not a role")
    elif isinstance(dsl, RequireApprovalAbove):
        if dsl.action is not None:
            need_write(dsl.action)
        floor = limits["money_limit"]["threshold_paise"]
        effective = dsl.amount_paise + (1 if dsl.comparison == "gt" else 0)       # smallest amount this rule escalates
        if effective >= floor:
            raise PolicyCompileError("NOT_TIGHTENING", f"escalating from {effective} paise does not lower the existing {floor} paise limit")
    elif isinstance(dsl, LowerWaiverBand):
        top = max(limits["waiver"]["role_cap_months"].values())
        if dsl.max_months >= top:
            raise PolicyCompileError("NOT_TIGHTENING", f"{dsl.max_months} months is not below the existing largest waiver cap of {top} months")


def validate(obj, registry: ActionRegistry, limits: dict) -> BaseModel:
    dsl = load_dsl(obj)
    check_tightens(dsl, registry, limits)
    return dsl


# -- DSL -> Rule (built here, from the DSL only) ---------------------------------------------------------
def _amount_at_risk(ctx: Ctx) -> int:
    amounts = []
    if isinstance(ctx.args.get("amount_paise"), int):
        amounts.append(ctx.args["amount_paise"])
    sid = ctx.args.get("settlement_id") or ctx.facts.get("settlement_id")
    if sid:
        row = ctx.conn.execute("SELECT amount_paise FROM settlements WHERE settlement_id = ?", (sid,)).fetchone()
        if row:
            amounts.append(row["amount_paise"])
    return max(amounts, default=0)


def build_rule(dsl: BaseModel) -> Rule:
    rid, h = rule_id_for(dsl), rule_hash(dsl)
    source = f"{SOURCE_DOC}; rule_hash {h}"        # the hash goes into the rule definition, so every compiled rule changes policy_hash
    if isinstance(dsl, BlockAction):
        who = f" for role {dsl.role}" if dsl.role else ""
        reason = f"{dsl.action!r}{who} is blocked by compiled rule {rid}"
        applies = {"actions": [dsl.action], **({"roles": [dsl.role]} if dsl.role else {})}
        return Rule(rid, f"Block {dsl.action}{who}", "BLOCK", source, lambda ctx: Finding(reason), applies)
    if isinstance(dsl, RequireApprovalAbove):
        floor = dsl.amount_paise + (1 if dsl.comparison == "gt" else 0)
        applies = {"principals": ["AGENT"], **({"actions": [dsl.action]} if dsl.action else {})}

        def cond(ctx: Ctx):
            amt = _amount_at_risk(ctx)
            if amt >= floor:
                return Finding(f"{amt} paise at risk reaches the compiled approval limit of {floor} paise ({rid})", route=ctx.limits["money_limit"]["route"])
            return None
        return Rule(rid, f"Approval from {floor} paise", "ESCALATE", source, cond, applies)
    if isinstance(dsl, LowerWaiverBand):
        cap = dsl.max_months

        def cond_w(ctx: Ctx):
            try:
                months = int(ctx.args.get("months"))
            except (TypeError, ValueError):
                return None                                  # WAIVER-BAND-001 already escalates malformed months
            if months > cap:
                return Finding(f"{months} months is above the compiled waiver band of {cap} ({rid})", route=ctx.limits["waiver"]["route"])
            return None
        return Rule(rid, f"Waiver band {cap} months", "ESCALATE", source, cond_w, {"principals": ["AGENT"], "actions": ["apply_fee_waiver"]})
    raise PolicyCompileError("INVALID_DSL", "unsupported template")


def describe(dsl: BaseModel) -> str:
    if isinstance(dsl, BlockAction):
        return f"Block {dsl.action}" + (f" for role {dsl.role}" if dsl.role else " for everyone")
    if isinstance(dsl, RequireApprovalAbove):
        op = "above" if dsl.comparison == "gt" else "at or above"
        return f"Escalate to ops_admin when an agent action moves {op} {dsl.amount_paise} paise" + (f" ({dsl.action})" if dsl.action else "")
    return f"Escalate fee waivers longer than {dsl.max_months} months"


# -- active rules (ledger is the only state) -------------------------------------------------------------
def active_compiled(conn: sqlite3.Connection) -> list[dict]:
    """[{rule_id, rule_hash, dsl, text, seq}] in approval order. Approved and not reverted since."""
    active: dict[str, dict] = {}
    rows = conn.execute("SELECT seq, payload_json FROM ledger WHERE kind = 'HUMAN_ACTION' AND json_extract(payload_json, '$.op') IN (?, ?) ORDER BY seq",
                        (OP_APPROVED, OP_REVERTED)).fetchall()
    for seq, pj in rows:
        p = json.loads(pj)
        if p["op"] == OP_APPROVED:
            active[p["rule_id"]] = {"rule_id": p["rule_id"], "rule_hash": p["rule_hash"], "dsl": p["dsl"], "text": p.get("text"), "seq": seq}
        else:
            active.pop(p["rule_id"], None)
    return list(active.values())


def overlay_rules(conn: sqlite3.Connection, registry: ActionRegistry, limits: dict) -> tuple[Rule, ...]:
    """Rebuild the active compiled rules. Each is re-validated, so a tampered ledger payload cannot load a non-DSL or relaxing rule."""
    out = []
    for a in active_compiled(conn):
        dsl = validate(a["dsl"], registry, limits)
        if rule_hash(dsl) != a["rule_hash"]:
            raise PolicyCompileError("HASH_MISMATCH", f"{a['rule_id']} does not match its recorded hash")
        out.append(build_rule(dsl))
    return tuple(out)


def base_rules(engine: PolicyEngine) -> tuple[Rule, ...]:
    return tuple(r for r in engine.rules if not r.id.startswith(RULE_PREFIX))


def sync_engine(engine: PolicyEngine, conn: sqlite3.Connection) -> str:
    """Make `engine` run exactly: its pack rules + the active compiled rules. Call at startup and after approve/revert. Returns the policy hash."""
    rules = base_rules(engine) + overlay_rules(conn, engine.registry, engine.limits)
    engine.rules = rules
    engine.policy_hash = compute_policy_hash(rules, engine.limits, engine.playbook_dir)
    return engine.policy_hash


# -- probes and the dry run ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Probe:
    id: str
    intent: ActionIntent
    source: str          # ledger | frozen | given


def probes_from_ledger(conn: sqlite3.Connection, limit: int = 200) -> list[Probe]:
    """Recent real attempts (cases and Judge Mode sessions alike): the ACTION_INTENT entries, replayed as agent intents."""
    out = []
    for seq, case_id, pj in conn.execute("SELECT seq, case_id, payload_json FROM ledger WHERE kind = 'ACTION_INTENT' ORDER BY seq DESC LIMIT ?", (limit,)).fetchall():
        p = json.loads(pj)
        out.append(Probe(f"L-{seq}", ActionIntent(p["action"], p.get("args") or {}, case_id=case_id, principal=Principal("AGENT", "munim"),
                                                 allowlist=(p["action"],), playbook_id=p.get("playbook_id"), step=p.get("step"), lane=p.get("lane")), "ledger"))
    return out


def probes_from_frozen(path: Path) -> list[Probe]:
    """The frozen #139 policy probes that need no special world setup (the ones that do are skipped here, not faked)."""
    if not Path(path).exists():
        return []
    out = []
    for p in json.loads(Path(path).read_text(encoding="utf-8"))["probes"]:
        if p.get("setup"):
            continue
        i = p["intent"]
        kind = i["principal"]
        pr = Principal("AGENT", "munim") if kind == "agent" else Principal("SYSTEM", "engine") if kind == "system" else \
            Principal("HUMAN", f"u-{kind.split(':', 1)[1] or 'x'}", kind.split(":", 1)[1] or None)
        allow = i["allow"]
        out.append(Probe(p["id"], ActionIntent(i["action"], i["args"], principal=pr, channel=i.get("channel"), facts=i.get("facts", {}),
                                               open_case_lanes=tuple(i.get("open_case_lanes", ())),
                                               allowlist=(i["action"],) if allow == "step" else None if allow == "none" else tuple(allow)), "frozen"))
    return out


def _engine_with(base: PolicyEngine, rules) -> PolicyEngine:
    return PolicyEngine(base.conn, base.clock, base.registry, rules, base.limits, playbook_dir=base.playbook_dir, reader=base.reader,
                        contact_budget=base.contact_budget)


def dry_run(base: PolicyEngine, candidate_rules, probes: list[Probe]) -> dict:
    """Run every probe on the current rule set and on the candidate. `loosened` lists probes whose outcome got WEAKER or whose BLOCK/ESCALATE
    rule ids disappeared; a non-empty list fails the draft. Pack rules removed from the candidate are reported too."""
    cur = _engine_with(base, base.rules)
    new = _engine_with(base, tuple(candidate_rules))
    changed, loosened = [], []
    removed = sorted({r.id for r in base.rules} - {r.id for r in candidate_rules})
    for pr in probes:
        a, b = cur.evaluate(pr.intent), new.evaluate(pr.intent)
        lost = sorted({v.rule_id for v in a.violations if v.severity != "WARN"} - {v.rule_id for v in b.violations if v.severity != "WARN"})
        row = {"probe": pr.id, "source": pr.source, "action": pr.intent.action, "before": a.outcome, "after": b.outcome,
               "new_rules": sorted({v.rule_id for v in b.violations} - {v.rule_id for v in a.violations})}
        if _OUTCOME_RANK[b.outcome] < _OUTCOME_RANK[a.outcome] or lost:
            loosened.append({**row, "lost_rules": lost})
        elif a.outcome != b.outcome:
            changed.append(row)
    return {"probes_run": len(probes), "changed": changed, "loosened": loosened, "removed_rules": removed,
            "ok": not loosened and not removed, "policy_hash_before": cur.policy_hash, "policy_hash_after": new.policy_hash}


# -- the draft -------------------------------------------------------------------------------------------
@dataclass
class Draft:
    text: str
    source: str                      # template | llm_proposal
    dsl: dict
    rule_id: str
    rule_hash: str
    summary: str
    diff: dict
    dry_run: dict
    ok: bool
    errors: list = field(default_factory=list)

    def as_json(self) -> dict:
        return {"text": self.text, "source": self.source, "dsl": self.dsl, "rule_id": self.rule_id, "rule_hash": self.rule_hash,
                "summary": self.summary, "diff": self.diff, "dry_run": self.dry_run, "ok": self.ok, "errors": self.errors, "label": "SIMULATED"}


def _diff(engine: PolicyEngine, rule: Rule, dsl: BaseModel, dr: dict) -> dict:
    d = rule.definition()
    return {"added": [{"rule_id": rule.id, "title": rule.title, "severity": rule.severity, "applies_to": d["applies_to"], "dsl": dsl.model_dump(exclude_none=True)}],
            "removed": dr["removed_rules"], "unchanged": sorted(r.id for r in engine.rules),
            "policy_hash_before": dr["policy_hash_before"], "policy_hash_after": dr["policy_hash_after"],
            "lines": [f"+ {rule.id} [{rule.severity}] {describe(dsl)}"] + [f"- {r}" for r in dr["removed_rules"]]}


def compile_text(text: str, engine: PolicyEngine, probes: list[Probe], *, phraser: Phraser | None = None) -> Draft:
    """Text -> validated, diffed, dry-run draft. Raises PolicyCompileError when the text cannot become a tightening rule.
    A draft whose dry run loosens anything comes back with ok=False and the evidence; it can never be approved."""
    reject_relaxing(text)                                    # before the LLM is consulted
    source = "template"
    try:
        obj = parse_text(text)
    except PolicyCompileError as e:
        if e.code != "NO_TEMPLATE" or phraser is None:
            raise
        obj = phraser.propose(text, actions=engine.registry.names(), roles=list(roles_mod.ROLES))
        if obj is None:
            raise
        source = "llm_proposal"
    return draft_from_dsl(obj, engine, probes, text=text, source=source)


def draft_from_dsl(obj, engine: PolicyEngine, probes: list[Probe], *, text: str = "", source: str = "template") -> Draft:
    dsl = validate(obj, engine.registry, engine.limits)
    rid = rule_id_for(dsl)
    if any(r.id == rid for r in engine.rules):
        raise PolicyCompileError("ALREADY_ACTIVE", f"{rid} is already in force")
    rule = build_rule(dsl)
    dr = dry_run(engine, tuple(engine.rules) + (rule,), probes)
    errors = [{"code": "DRY_RUN_LOOSENED", "probe": x["probe"]} for x in dr["loosened"]]
    return Draft(text, source, dsl.model_dump(exclude_none=True), rid, rule_hash(dsl), describe(dsl), _diff(engine, rule, dsl, dr), dr, dr["ok"], errors)


# -- approve / revert (ops_admin, through the ledger) ----------------------------------------------------
def _require_ops(principal: Principal) -> None:
    if principal.kind != "HUMAN" or principal.role != APPROVER_ROLE:
        raise PolicyCompileError("NOT_OPS_ADMIN", f"only role {APPROVER_ROLE} approves or reverts compiled rules")


def approve(engine: PolicyEngine, ledger: Ledger, obj: dict, principal: Principal, *, expected_hash: str, probes: list[Probe], text: str = "") -> dict:
    """Re-validates and re-runs the dry run (nothing is trusted from the client), requires the exact hash the operator saw, then ledgers and syncs."""
    _require_ops(principal)
    d = draft_from_dsl(obj, engine, probes, text=text)
    if d.rule_hash != expected_hash:
        raise PolicyCompileError("HASH_MISMATCH", "the rule changed since it was shown; compile again")
    if not d.ok:
        raise PolicyCompileError("DRY_RUN_LOOSENED", "the dry run weakened an outcome; not approved")
    e = ledger.append(kind="HUMAN_ACTION", actor=principal.ledger_actor, role=principal.role,
                      payload={"op": OP_APPROVED, "rule_id": d.rule_id, "rule_hash": d.rule_hash, "dsl": d.dsl, "text": text,
                               "probes_run": d.dry_run["probes_run"], "changed": len(d.dry_run["changed"]),
                               "policy_hash_before": d.dry_run["policy_hash_before"], "label": "SIMULATED"})
    new_hash = sync_engine(engine, ledger.conn)
    return {"rule_id": d.rule_id, "rule_hash": d.rule_hash, "ledger_seq": e.seq, "policy_hash": new_hash, "policy_hash_before": d.dry_run["policy_hash_before"]}


def revert(engine: PolicyEngine, ledger: Ledger, rule_id: str, principal: Principal) -> dict:
    """One-click revert of a compiled rule. Pack rules are not revertable here (they are not in the ledger-backed set)."""
    _require_ops(principal)
    if not any(a["rule_id"] == rule_id for a in active_compiled(ledger.conn)):
        raise PolicyCompileError("NOT_ACTIVE", f"{rule_id} is not an active compiled rule")
    before = engine.policy_hash
    e = ledger.append(kind="HUMAN_ACTION", actor=principal.ledger_actor, role=principal.role,
                      payload={"op": OP_REVERTED, "rule_id": rule_id, "policy_hash_before": before, "label": "SIMULATED"})
    return {"rule_id": rule_id, "ledger_seq": e.seq, "policy_hash": sync_engine(engine, ledger.conn), "policy_hash_before": before}
