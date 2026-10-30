"""Human roles and their permission matrix (Bible section 30).

The matrix says which registered actions a role may perform (and therefore approve). It is consulted ONLY by the
ROLE-001 rule inside the policy engine: there is no second code path and no admin bypass. Every other rule still
runs for every human, whatever the role.
"""
from __future__ import annotations

from typing import Iterable

from core.actions.registry import ActionRegistry, RiskClass

ROLES = ("support_executive", "risk_reviewer", "sales_lead", "ops_admin", "auditor", "area_manager", "field_rep", "technician")

# Undo/compensation actions, by who may run them (Bible: "Can Undo" column).
_UNDO_SUPPORT = {"cancel_follow_up", "cancel_booking", "restore_preference", "withdraw_ticket", "cancel_device_replacement",
                 "cancel_device_order", "cancel_refund_request", "cancel_closure_request", "revoke_fee_waiver", "revert_plan_change",
                 "rollback_device_config"}
_UNDO_SALES = {"revoke_fee_waiver", "revert_plan_change", "cancel_device_order", "cancel_follow_up", "cancel_booking"}
_UNDO_RISK = {"withdraw_ticket", "cancel_refund_request", "cancel_closure_request", "cancel_follow_up", "cancel_booking"}
_UNDO_AREA = {"cancel_follow_up", "cancel_booking", "revoke_fee_waiver"}

# role -> write actions it may perform. Reads and drafts are open to every known role (auditor included).
PERMISSIONS: dict[str, frozenset[str]] = {
    "support_executive": frozenset({
        "schedule_follow_up", "book_visit_or_call", "save_preference", "raise_ticket", "create_device_replacement",
        "create_refund_request", "create_closure_request", "apply_fee_waiver", "send_message", "send_kyc_resume_link",
        "send_bank_update_link", "retrigger_bank_verification"} | _UNDO_SUPPORT),
    "risk_reviewer": frozenset({"clear_risk_hold", "raise_ticket", "send_message"} | _UNDO_RISK),
    "sales_lead": frozenset({"apply_fee_waiver", "create_plan_change", "create_device_order", "send_partner_application_link",
                             "send_message", "schedule_follow_up"} | _UNDO_SALES),
    "area_manager": frozenset({"apply_fee_waiver", "book_visit_or_call", "schedule_follow_up"} | _UNDO_AREA),
    "field_rep": frozenset({"book_visit_or_call", "schedule_follow_up", "save_preference"}),
    "technician": frozenset({"raise_ticket", "create_device_replacement", "book_visit_or_call"}),
    "auditor": frozenset(),
}
# ops_admin: any registered write EXCEPT the ones reserved to another role. Still subject to every other rule.
OPS_ADMIN_EXCLUDED = frozenset({"clear_risk_hold"})
RESERVED = {"clear_risk_hold": "risk_reviewer"}


def may_perform(role: str | None, action: str, registry: ActionRegistry) -> bool:
    """True when `role` may perform `action`. Unknown roles and unregistered actions get False."""
    if not role or role not in ROLES or not registry.has(action):
        return False
    spec = registry.get(action)
    if spec.cls in (RiskClass.R0, RiskClass.DRAFT):
        return True
    if role == "ops_admin":
        return action not in OPS_ADMIN_EXCLUDED
    return action in PERMISSIONS.get(role, frozenset())


def can_approve(role: str | None, action: str, registry: ActionRegistry, required_roles: Iterable[str]) -> bool:
    """A role approves an escalated action only if it is a named approver AND may itself perform the action."""
    return role in set(required_roles) and may_perform(role, action, registry)
