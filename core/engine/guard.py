"""Decision guard (#38): nothing the model proposes reaches the engine, and so the executor, unvalidated.

``DecisionGuard.check`` takes the model's ``TurnDecision`` and returns an engine ``Decision`` that may have had its action dropped,
plus what the engine must do about it (escalate, park). It layers on top of, never instead of, the engine's allowlist/fix-first gate
and the executor's policy check; it exists so a bad proposal is rejected early, explained ("Kyun nahi?") and counted.

Checks on ``next_action`` (all findings are collected; any finding drops the action):
  UNKNOWN-ACTION        not a registered agent-callable action
  NOT-ALLOWLISTED       not in this step's allowlist (fix-first has already removed offer actions from it)
  FIX-FIRST-001         an offer action while a P0/P1 case is open: dropped and the case is parked
  MODEL-WRITTEN-TEXT    a contact action carrying text the model wrote (merchant-facing words come only from the renderer)
  INVALID-ARGS          fails the action's argument schema
  WRONG-SUBJECT         merchant_id / case_id other than this case's: dropped and escalated
  ARG-FROM-MERCHANT     a number in the args that is in the merchant's message and in NO system source this turn: dropped and escalated
  <policy rule ids>     a dry run of the real policy engine against live reads (offer band, consent, eligibility, KYC-in-chat ...):
                        BLOCK drops the action, ESCALATE also escalates. The executor still re-evaluates at execution time.
``new_case_requests`` become OPEN / PARK / LINK / REJECT dispositions; offers requested during fix-first are PARKED, never opened live.
Nothing here trusts a number because the model copied it: arguments are checked against system reads and system sources.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from pydantic import ValidationError

from core.actions.types import ActionIntent
from core.engine.case_manager import FIX_FIRST_RULE, CaseManager, fix_first_active
from core.engine.workflow import MERCHANT_INITIATED, Decision as EngineDecision, DecisionContext
from core.gate.numbers import extract_numbers, number_key
from core.gate.sources import source_numbers
from core.llm.decision import TurnDecision

TEXT_ARG_KEYS = ("body", "text", "message", "message_en")
_ID_KEY = re.compile(r"(_id|_at|_ref|reference|key)$")
MAX_NEW_CASES = 2
MERCHANT_AUTHORED_FACTS = frozenset({"event", "text", "merchant_message"})

# finding kinds: what the engine must do besides dropping the action
DROP, ESCALATE, PARK = "DROP", "ESCALATE", "PARK"


@dataclass(frozen=True)
class GuardFinding:
    rule_id: str
    reason: str
    kind: str = DROP


@dataclass(frozen=True)
class NewCaseDisposition:
    playbook_id: str
    disposition: str                 # OPEN | PARK | LINK | REJECT
    case_id: str | None = None       # the existing case for LINK
    reason: str = ""


@dataclass
class GuardResult:
    decision: EngineDecision                          # what the engine may act on (action None if it was dropped)
    findings: list[GuardFinding] = field(default_factory=list)
    new_cases: list[NewCaseDisposition] = field(default_factory=list)
    dropped_action: str | None = None
    ledger_seqs: list[int] = field(default_factory=list)

    @property
    def escalate_reasons(self) -> list[str]:
        return [f"{f.rule_id}: {f.reason}" for f in self.findings if f.kind == ESCALATE]

    @property
    def park(self) -> bool:
        return any(f.kind == PARK for f in self.findings)


def _arg_numbers(args: Mapping[str, Any]) -> list[tuple[str, tuple[str, object]]]:
    """(arg path, comparison key) for every numeric value in the args, ids and timestamps excluded. ``*_paise`` is compared in rupees."""
    out: list[tuple[str, tuple[str, object]]] = []

    def walk(v: Any, key: str, path: str) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, str(k), f"{path}.{k}" if path else str(k))
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                walk(x, key, f"{path}[{i}]")
        elif isinstance(v, (int, float, Decimal)):
            if _ID_KEY.search(key.lower()):
                return
            d = Decimal(str(v))
            if key.lower().endswith("_paise"):
                d = d / 100
            out.append((path, ("num", d.normalize() if d != 0 else Decimal(0))))
        elif isinstance(v, str) and len(v) <= 60 and not _ID_KEY.search(key.lower()):
            for n in extract_numbers(v):
                if n.kind not in ("date", "time"):
                    out.append((path, number_key(n)))

    walk(args, "", "")
    return out


class DecisionGuard:
    def __init__(self, registry, case_manager: CaseManager, reader, policy, playbooks: Mapping[str, Any], ledger=None, *,
                 max_new_cases: int = MAX_NEW_CASES):
        self.registry, self.case_manager, self.reader, self.policy = registry, case_manager, reader, policy
        self.playbooks, self.ledger, self.max_new_cases = playbooks, ledger, max_new_cases

    # ---------------------------------------------------------------------------------------------------------------
    def check(self, ctx: DecisionContext, td: TurnDecision, merchant_message: str = "", sources: Mapping[str, Any] | None = None) -> GuardResult:
        findings: list[GuardFinding] = []
        action: tuple[str, dict] | None = None
        name = None
        if td.next_action is not None:
            name = td.next_action.name
            action, findings = self._check_action(ctx, td, merchant_message, sources or {})
        if td.ask_clarification and action is not None:
            findings.append(GuardFinding("CLARIFY-NO-ACTION", f"{name}: the model asked a clarifying question, so no action runs this turn"))
            action = None
        if td.focus_case and td.focus_case not in {c.case_id for c in ctx.open_cases}:
            findings.append(GuardFinding("UNKNOWN-CASE", f"focus_case {td.focus_case!r} is not an open case of this merchant; the engine chooses focus"))
        new_cases = self._new_cases(ctx, td)
        result = GuardResult(EngineDecision(action=action), findings, new_cases, dropped_action=name if (name and action is None) else None)
        self._ledger(ctx, result)
        return result

    # ---------------------------------------------------------------------------------------------------------------
    def _check_action(self, ctx: DecisionContext, td: TurnDecision, merchant_message: str, sources: Mapping[str, Any]) -> tuple[tuple[str, dict] | None, list[GuardFinding]]:
        name, args = td.next_action.name, dict(td.next_action.args)
        reg, case = self.registry, ctx.case
        if not reg.has(name) or not reg.get(name).agent_callable:
            return None, [GuardFinding("UNKNOWN-ACTION", f"{name} is not a registered agent action")]
        spec = reg.get(name)
        lanes = tuple(c.lane for c in ctx.open_cases)
        proactive = spec.contact and ctx.event.type not in MERCHANT_INITIATED
        verdict = self.case_manager.intent_allowed(name, ctx.allowlist, lanes, case_lane=case.lane, proactive=proactive)
        if not verdict.ok:
            kind = PARK if verdict.rule_id == FIX_FIRST_RULE else DROP
            return None, [GuardFinding(verdict.rule_id or "NOT-ALLOWLISTED", verdict.reason or "", kind)]

        found: list[GuardFinding] = []
        if spec.contact and any(isinstance(args.get(k), str) and args[k].strip() for k in TEXT_ARG_KEYS):
            found.append(GuardFinding("MODEL-WRITTEN-TEXT", f"{name} carries text written by the model; merchant-facing words come only from the renderer"))
        if found:
            return None, found           # the words are dropped anyway, so a stray id beside them is not worth an escalation: the writer sends the reply to this case's own merchant
        subject = case.subject_id
        fields = spec.args_model.model_fields
        for key, want in (("merchant_id", subject), ("case_id", case.case_id)):
            if key in fields:
                if key in args and args[key] != want:
                    found.append(GuardFinding("WRONG-SUBJECT", f"{key} {args[key]!r} is not this case's ({want!r})", ESCALATE))
                args[key] = want                                         # system-controlled: always the case's own
        try:
            args = spec.args_model(**args).model_dump(exclude_none=True)
        except ValidationError as e:
            found.append(GuardFinding("INVALID-ARGS", "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:300]))
        found += self._provenance(args, merchant_message, ctx, sources)
        if found:
            return None, found

        intent = ActionIntent(action=name, args=args, case_id=case.case_id, step_seq=0, playbook_id=case.playbook_id, step=getattr(ctx.step, "name", None),
                              proactive=proactive, lane=case.lane, open_case_lanes=lanes, allowlist=ctx.allowlist,
                              channel=(args.get("channel") or ctx.event.payload.get("channel") or "whatsapp") if spec.contact else None,
                              facts=dict(ctx.facts))
        decision = self.policy.evaluate(intent, self.reader)              # a dry run: nothing is reserved, sent or ledgered by it
        if decision.outcome != "ALLOW":
            for v in decision.violations:
                if v.severity == "WARN":
                    continue
                kind = PARK if v.rule_id == FIX_FIRST_RULE else (ESCALATE if v.severity == "ESCALATE" or v.rule_id.startswith(("WAIVER", "MONEY", "RISK")) else DROP)
                found.append(GuardFinding(v.rule_id, v.reason, kind))
            return None, found
        return (name, args), []

    def _provenance(self, args: dict, merchant_message: str, ctx: DecisionContext, sources: Mapping[str, Any]) -> list[GuardFinding]:
        said = {number_key(n) for n in extract_numbers(merchant_message or "")}
        if not said:
            return []
        # the engine's facts carry the triggering event's payload, i.e. the merchant's own words: never count those as a system source
        system = source_numbers({k: v for k, v in dict(ctx.facts).items() if k not in MERCHANT_AUTHORED_FACTS})
        for body in sources.values():
            system |= source_numbers(body)
        out = []
        for path, key in _arg_numbers(args):
            if key in said and key not in system:
                out.append(GuardFinding("ARG-FROM-MERCHANT", f"argument {path or 'value'} is a number the merchant wrote and no system source supports", ESCALATE))
        return out

    def _new_cases(self, ctx: DecisionContext, td: TurnDecision) -> list[NewCaseDisposition]:
        out: list[NewCaseDisposition] = []
        ff = fix_first_active(ctx.open_cases)
        seen: set[str] = set()
        opened = 0
        for req in td.new_case_requests:
            pid = req.playbook_id
            if pid in seen:
                continue
            seen.add(pid)
            pb = self.playbooks.get(pid)
            if pb is None:
                out.append(NewCaseDisposition(pid, "REJECT", reason=f"{pid} is not a known playbook"))
                continue
            existing = next((c for c in ctx.open_cases if c.playbook_id == pid), None)
            if existing is not None:
                out.append(NewCaseDisposition(pid, "LINK", existing.case_id, "an open case of this playbook already exists"))
            elif opened >= self.max_new_cases:
                out.append(NewCaseDisposition(pid, "REJECT", reason=f"at most {self.max_new_cases} new cases per turn"))
            elif ff and pb.priority_lane == "P3":
                opened += 1
                out.append(NewCaseDisposition(pid, "PARK", reason="fix-first: an offer case is opened parked while a P0/P1 case is open"))
            else:
                opened += 1
                out.append(NewCaseDisposition(pid, "OPEN"))
        return out

    def _ledger(self, ctx: DecisionContext, result: GuardResult) -> None:
        if self.ledger is None:
            return
        refusals = [f for f in result.findings if f.rule_id not in ("UNKNOWN-CASE", "CLARIFY-NO-ACTION")]
        if refusals:
            outcome = ESCALATE if any(f.kind == ESCALATE for f in refusals) else (PARK if any(f.kind == PARK for f in refusals) else DROP)
            seq = self.ledger.append(kind="POLICY", actor="SYSTEM", case_id=ctx.case.case_id, payload={
                "action": result.dropped_action, "allow": False, "rule_ids": [f.rule_id for f in refusals], "reasons": [f.reason for f in refusals],
                "outcome": outcome, "step": getattr(ctx.step, "name", None), "source": "guard"}).seq
            result.ledger_seqs.append(seq)
