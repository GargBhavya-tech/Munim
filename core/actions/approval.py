"""Server-built approval views (Bible section 30: "Approval views are built from server data only").

Every field of the view is derived from the stored ActionIntent, the registry, the policy decision and reader snapshots taken when
the approval was created. The model's explanation is stored under its own key and is never read by anything that builds an
action field. ``decide`` enforces the approver role on the server; the executor is NOT called here: ``on_approve`` is injected.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from core.actions.registry import ActionRegistry, RiskClass
from core.actions.types import ActionIntent, PolicyDecision, Principal, Reader, Violation
from core.engine import roles as roles_mod
from core.engine.clock import iso
from core.memory.store import next_id, write_txn

EXPLANATION_LABEL = "Agent's explanation (written by the model, unverified)"
Verdict = str  # "approve" | "reject"


def _registry(registry: ActionRegistry | None) -> ActionRegistry:
    if registry is not None:
        return registry
    from packs.paytm_merchant.actions import build_registry  # default pack; callers may inject their own
    return build_registry()


def _intent_dict(i: ActionIntent) -> dict:
    return {"action": i.action, "args": i.args, "case_id": i.case_id, "step_seq": i.step_seq,
            "principal": {"kind": i.principal.kind, "id": i.principal.id, "role": i.principal.role},
            "playbook_id": i.playbook_id, "step": i.step, "proactive": i.proactive, "lane": i.lane,
            "open_case_lanes": list(i.open_case_lanes), "channel": i.channel, "facts": i.facts}


def intent_from_dict(d: dict, *, principal: Principal | None = None, args: dict | None = None) -> ActionIntent:
    p = d["principal"]
    return ActionIntent(action=d["action"], args=args if args is not None else d["args"], case_id=d["case_id"], step_seq=d["step_seq"],
                        principal=principal or Principal(p["kind"], p["id"], p["role"]), playbook_id=d["playbook_id"], step=d["step"],
                        proactive=d["proactive"], lane=d["lane"], open_case_lanes=tuple(d["open_case_lanes"]), channel=d["channel"],
                        allowlist=None, facts=d["facts"])


# What to read for "state before", per action. Names are R0 actions the Reader understands.
_BEFORE = {
    "apply_fee_waiver": [("offer_band", "get_offer_band")],
    "create_plan_change": [("plan", "get_plan")],
    "create_refund_request": [("refunds", "get_refunds")],
    "create_device_replacement": [("device", "get_device_status")],
    "create_device_order": [("device", "get_device_status")],
    "send_partner_application_link": [("eligibility", "check_partner_eligibility")],
    "create_closure_request": [("merchant", "get_merchant")],
    "send_bank_update_link": [("bank", "get_bank_status")],
    "retrigger_bank_verification": [("bank", "get_bank_status")],
    "send_kyc_resume_link": [("kyc", "get_kyc_status")],
}


def _state_before(conn, intent: ActionIntent, reader: Reader | None) -> dict:
    out: dict = {}
    mid = intent.args.get("merchant_id") or intent.facts.get("merchant_id")
    sid = intent.args.get("settlement_id")
    if sid:
        row = conn.execute("SELECT settlement_id, merchant_id, amount_paise, status, reason FROM settlements WHERE settlement_id = ?", (sid,)).fetchone()
        out["settlement"] = dict(row) if row else {"settlement_id": sid, "status": "NOT_FOUND"}
        mid = mid or (row["merchant_id"] if row else None)
    for label, read in _BEFORE.get(intent.action, []):
        if reader is None or not mid:
            out[label] = {"error": "unavailable"}
            continue
        try:
            out[label] = reader(read, {"merchant_id": mid})
        except Exception as e:
            out[label] = {"error": f"unavailable ({type(e).__name__})"}
    return out


def _state_after(intent: ActionIntent, before: dict) -> dict:
    a, args = intent.action, intent.args
    if a == "apply_fee_waiver":
        return {"fee_waiver_months": args.get("months"), "offer_id": args.get("offer_id", "FEE_WAIVER_1M")}
    if a == "clear_risk_hold":
        return {"settlement": {**before.get("settlement", {}), "status": "SETTLED"}}
    if a == "create_refund_request":
        return {"refund_amount_paise": args.get("amount_paise"), "payment_id": args.get("payment_id")}
    return {"effect": f"{a} runs with the arguments shown"}


@dataclass
class DecideResult:
    status: str                     # APPROVED | REJECTED | PENDING (more roles needed) | REFUSED
    approval_id: str
    reason: str = ""
    violation: Violation | None = None


def create_approval(conn: sqlite3.Connection, clock, intent: ActionIntent, decision: PolicyDecision, reader: Reader | None,
                    agent_explanation: str | None = None, *, registry: ActionRegistry | None = None) -> str:
    if decision.outcome != "ESCALATE":
        raise ValueError(f"only an ESCALATE decision needs an approval, got {decision.outcome}")
    reg = _registry(registry)
    spec = reg.get(intent.action)
    esc = [v for v in decision.violations if v.severity == "ESCALATE"]
    required = sorted({v.route for v in esc if v.route})
    if not required:
        raise ValueError("ESCALATE decision names no route")
    before = _state_before(conn, intent, reader)
    window = spec.window_hours
    now = clock.now()
    comp = {"action": spec.compensation, "window_hours": window,
            "until": iso(now + timedelta(hours=window)) if window else None,
            "note": ("irreversible" if spec.cls is RiskClass.R3 else "compensable while the object is open" if spec.compensation and not window else None)}
    view = {
        "action": intent.action, "risk_class": spec.cls.value, "args": intent.args, "case_id": intent.case_id,
        "requested_by": f"{intent.principal.kind}:{intent.principal.id}", "state_before": before, "state_after_if_approved": _state_after(intent, before),
        "rules_fired": [{"rule_id": v.rule_id, "title": v.title, "severity": v.severity, "source": v.source, "reason": v.reason, "route": v.route}
                        for v in decision.violations],
        "compensation": comp, "evidence_ids": list(intent.facts.get("evidence_ids", [])), "policy_hash": decision.policy_hash,
        "required_roles": required, "decisions": [], "created_at": clock.now_iso(),
        "intent": _intent_dict(intent),
        "agent_explanation": agent_explanation,
    }
    with write_txn(conn):
        aid = next_id(conn, "AP")
        conn.execute("INSERT INTO approvals(approval_id, case_id, action_id, status, view_json, role_required, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                     (aid, intent.case_id, None, "PENDING", json.dumps(view, sort_keys=True), ",".join(required), None, None))
    return aid


def _load(conn, approval_id: str):
    row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
    if row is None:
        raise KeyError(approval_id)
    return row, json.loads(row["view_json"])


def _summary(v: dict) -> str:
    """The approval's headline: the action and its server-recorded args. Never the model's text."""
    args_text = ", ".join(f"{k}={v['args'][k]}" for k in sorted(v["args"]))
    return f"{v['action']} ({args_text})"


def build_approval_view(conn: sqlite3.Connection, approval_id: str) -> dict:
    """The dialog payload. Action fields come from server records only; the model's text is in `agent_explanation` and nowhere else."""
    row, v = _load(conn, approval_id)
    return {
        "approval_id": approval_id, "status": row["status"],
        "action": v["action"], "risk_class": v["risk_class"], "args": v["args"],
        "summary": _summary(v),
        "case_id": v["case_id"], "requested_by": v["requested_by"],
        "state_before": v["state_before"], "state_after_if_approved": v["state_after_if_approved"],
        "rules_fired": v["rules_fired"], "compensation": v["compensation"], "evidence_ids": v["evidence_ids"],
        "policy_hash": v["policy_hash"], "required_roles": v["required_roles"], "decisions": v["decisions"],
        "agent_explanation": {"label": EXPLANATION_LABEL, "text": v["agent_explanation"]},
    }


def decide(conn: sqlite3.Connection, approval_id: str, principal: Principal, verdict: Verdict,
           on_approve: Callable[[ActionIntent], object] | None, *, clock, registry: ActionRegistry | None = None,
           approved_args: dict | None = None) -> DecideResult:
    """Server-side role check. A wrong role gets REFUSED with a ROLE-001 violation and changes nothing. On the final approval
    `on_approve(intent)` is called with the intent to submit to the executor under the approving human's principal (the executor
    re-runs policy, so a human can never approve past their own limits). `approved_args` lets the approver reduce the request
    (for example 6 months -> 1 month); it replaces the args of the submitted intent only and is recorded in the decision."""
    reg = _registry(registry)
    with write_txn(conn):
        row, v = _load(conn, approval_id)
        h = v["policy_hash"]

        def refuse(reason: str, rule_id: str = "ROLE-001") -> DecideResult:
            return DecideResult("REFUSED", approval_id, reason, Violation(rule_id, "A human action must be within the actor's role", "BLOCK",
                                reason, "MUNIM_Bible_Merged.md section 30", v["action"], v["case_id"], h))

        if row["status"] != "PENDING":
            return refuse(f"approval already {row['status']}", "APPROVAL-CLOSED")
        if verdict not in ("approve", "reject"):
            return refuse(f"unknown verdict {verdict!r}", "APPROVAL-BAD-VERDICT")
        done = {d["role"] for d in v["decisions"]}
        remaining = [r for r in v["required_roles"] if r not in done]
        if principal.kind != "HUMAN" or not roles_mod.can_approve(principal.role, v["action"], reg, remaining):
            return refuse(f"role {principal.role!r} ({principal.kind}) may not decide this approval; needs one of {remaining}")
        v["decisions"].append({"by": principal.id, "role": principal.role, "verdict": verdict, "at": clock.now_iso(),
                               "approved_args": approved_args if verdict == "approve" else None})
        if verdict == "reject":
            status = "REJECTED"
        elif len(remaining) > 1:
            status = "PENDING"
        else:
            status = "APPROVED"
        final = status != "PENDING"
        conn.execute("UPDATE approvals SET status=?, view_json=?, decided_by=?, decided_at=? WHERE approval_id=?",
                     (status, json.dumps(v, sort_keys=True), principal.id if final else None, clock.now_iso() if final else None, approval_id))
    if status == "APPROVED" and on_approve is not None:
        final_args = next((d["approved_args"] for d in reversed(v["decisions"]) if d["approved_args"]), None)
        on_approve(intent_from_dict(v["intent"], principal=principal, args=final_args))
    return DecideResult(status, approval_id, "")
