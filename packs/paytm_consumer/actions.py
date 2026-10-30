"""Consumer pack actions (#89). The subject argument is ``merchant_id`` because that is the engine's subject field; here it is a consumer id."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass

R0, R1, R2, R3 = RiskClass.R0, RiskClass.R1, RiskClass.R2, RiskClass.R3


class Subject(BaseModel):
    merchant_id: str = Field(pattern=r"^U-\d{4}$")          # a consumer id


class Dispute(Subject):
    txn_id: str


class DisputeRef(Subject):
    dispute_id: str


class Message(Subject):
    channel: str
    body: str
    language: Optional[str] = None
    case_id: Optional[str] = None


SPECS = [
    ActionSpec("get_upi_transactions", R0, Subject, "upi", "GET", "/mock/consumer/{merchant_id}/transactions"),
    ActionSpec("get_consumer_refunds", R0, Subject, "upi", "GET", "/mock/consumer/{merchant_id}/refunds"),
    ActionSpec("raise_upi_dispute", R2, Dispute, "upi", "POST", "/mock/consumer/{merchant_id}/disputes", ("txn_id",),
               compensation="withdraw_upi_dispute", window_hours=24,
               comp_args=lambda args, result: {"merchant_id": args["merchant_id"], "dispute_id": result["dispute_id"]}),
    ActionSpec("withdraw_upi_dispute", R1, DisputeRef, "upi", "DELETE", "/mock/consumer/{merchant_id}/disputes/{dispute_id}", agent_callable=False),
    ActionSpec("send_message", R3, Message, "inbox", "POST", "/mock/consumer/{merchant_id}/messages", ("channel", "body", "language", "case_id"), contact=True),
]


def build_registry() -> ActionRegistry:
    return ActionRegistry(SPECS)
