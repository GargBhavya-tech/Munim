"""Approval endpoints. A standalone router: the integrator mounts it and sets app.state.conn / clock / on_approve (and optionally registry).

Identity is taken from the request body here because no auth layer exists yet; the ROLE CHECK is still done on the server in
core.actions.approval.decide, never trusted from the client UI.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.actions.approval import build_approval_view, decide
from core.actions.types import Principal

router = APIRouter(prefix="/approvals")


class DecideIn(BaseModel):
    verdict: str
    principal_id: str
    role: Optional[str] = None
    approved_args: Optional[dict] = None


@router.get("/{approval_id}")
def get_approval(approval_id: str, request: Request):
    try:
        return build_approval_view(request.app.state.conn, approval_id)
    except KeyError:
        raise HTTPException(404, "APPROVAL_NOT_FOUND")


@router.post("/{approval_id}/decide")
def post_decide(approval_id: str, body: DecideIn, request: Request):
    st = request.app.state
    try:
        res = decide(st.conn, approval_id, Principal("HUMAN", body.principal_id, body.role), body.verdict,
                     getattr(st, "on_approve", None), clock=st.clock, registry=getattr(st, "registry", None), approved_args=body.approved_args)
    except KeyError:
        raise HTTPException(404, "APPROVAL_NOT_FOUND")
    if res.status == "REFUSED":
        code = 409 if res.violation and res.violation.rule_id == "APPROVAL-CLOSED" else 403
        return JSONResponse({"status": "REFUSED", "rule_id": res.violation.rule_id, "reason": res.reason,
                             "policy_hash": res.violation.policy_hash}, status_code=code)
    return {"status": res.status, "approval_id": approval_id}
