"""Shared contracts between the case engine, the policy engine and the action executor.

Three teams build against these at once, so changing a field name or meaning here is a breaking change:
ask the integrator first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

# R0 reads are performed through a Reader so every consumer (diagnose, policy, guard) reads live system
# state and none of them calls a mock directly. reader("get_offer_band", {"merchant_id": "M-0001"}) -> body dict.
Reader = Callable[[str, dict], dict]

Role = Literal["support_executive", "risk_reviewer", "sales_lead", "ops_admin", "auditor", "area_manager", "field_rep", "technician"]
Severity = Literal["BLOCK", "ESCALATE", "WARN"]
Outcome = Literal["ALLOW", "BLOCK", "ESCALATE"]
Status = Literal["OK", "REFUSED", "ESCALATE", "ERROR", "REPLAYED"]


@dataclass(frozen=True)
class Principal:
    kind: Literal["AGENT", "HUMAN", "SYSTEM"]
    id: str = "engine"
    role: str | None = None            # set for HUMAN

    @property
    def ledger_actor(self) -> str:
        return f"HUMAN:{self.id}" if self.kind == "HUMAN" else self.kind


AGENT = Principal("AGENT", "munim")


@dataclass(frozen=True)
class ActionIntent:
    action: str                         # a name in the ActionRegistry
    args: dict
    case_id: str | None = None
    step_seq: int = 0                   # position in the case; part of the idempotency key
    principal: Principal = AGENT
    playbook_id: str | None = None
    step: str | None = None             # current playbook step name
    proactive: bool = False             # True when Munim starts the contact (quiet hours / caps / consent apply)
    lane: str | None = None             # P0..P3 of the case this action belongs to
    open_case_lanes: tuple[str, ...] = ()  # lanes of ALL open cases for this merchant, for fix-first
    channel: str | None = None          # for contact actions: app | soundbox | whatsapp | sms | voice | email
    allowlist: tuple[str, ...] | None = None   # actions the current step permits (after fix-first). None = not applicable (human/system)
    facts: dict = field(default_factory=dict)  # case facts the engine already knows, for rule inputs. Keys in use:
    #   closure_requests (int), kfs_message_sent (bool), replacements_last_30d (int), mandate_month_total_paise, mandate_limit_paise,
    #   mandate_purpose, targeting ("individual_rival"), via_mcp (bool), analysis (bool), claims_blocked (int)


@dataclass(frozen=True)
class Violation:
    rule_id: str
    title: str
    severity: Severity
    reason: str
    source: str = ""                    # the document and section the rule comes from
    action: str | None = None
    case_id: str | None = None
    policy_hash: str = ""
    route: str | None = None            # which human role should look at it, for ESCALATE


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    violations: tuple[Violation, ...] = ()
    policy_hash: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "ALLOW"


@dataclass
class ActionOutcome:
    status: Status
    idempotency_key: str
    action_id: str | None = None
    result: dict | None = None          # the mock's response body
    violations: list[Violation] = field(default_factory=list)
    compensable_until: str | None = None
    error: str | None = None


class PolicyEngine(Protocol):
    policy_hash: str

    def evaluate(self, intent: ActionIntent, reader: Reader | None = None) -> PolicyDecision: ...


class Executor(Protocol):
    def submit(self, intent: ActionIntent) -> ActionOutcome: ...
    def undo(self, action_id: str, principal: Principal) -> ActionOutcome: ...


class ContactBudgetLike(Protocol):
    """Implemented by core/engine/contact_budget.py (#22); consumed by the CONTACT-CAP-001 policy rule."""

    def check(self, subject_id: str, channel: str, *, proactive: bool, case_id: str | None = None) -> tuple[bool, str | None]: ...

    def record(self, subject_id: str, channel: str, *, case_id: str | None, direction: str = "OUT") -> None: ...
