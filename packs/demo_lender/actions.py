"""Lender-collections actions: three tools (read the account, record a consented promise-to-pay, send a message) plus the promise's
compensation. The subject argument is ``merchant_id`` because that is the engine's subject field; here it is a borrower id.
There is no tool to change a loan, waive a charge, take a payment or pass anything to a third party: those do not exist."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass

R0, R1, R2, R3 = RiskClass.R0, RiskClass.R1, RiskClass.R2, RiskClass.R3


class Subject(BaseModel):
    merchant_id: str = Field(pattern=r"^B-\d{4}$")          # a borrower id


class Promise(Subject):
    promised_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    borrower_consented: bool = False                          # the borrower said it themselves; checked by policy rule D-PTP-001


class PromiseRef(Subject):
    promise_id: str


class Message(Subject):
    channel: str
    body: str
    language: Optional[str] = None
    case_id: Optional[str] = None
    recipient: str = "borrower"                               # anything else is refused by D-PRIVACY-001


SPECS = [
    ActionSpec("get_loan_account", R0, Subject, "lender", "GET", "/mock/lender/{merchant_id}/loan"),
    ActionSpec("record_promise_to_pay", R2, Promise, "lender", "POST", "/mock/lender/{merchant_id}/promises", ("promised_on", "borrower_consented"),
               compensation="cancel_promise_to_pay", window_hours=24,
               comp_args=lambda args, result: {"merchant_id": args["merchant_id"], "promise_id": result["promise_id"]}),
    ActionSpec("cancel_promise_to_pay", R1, PromiseRef, "lender", "DELETE", "/mock/lender/{merchant_id}/promises/{promise_id}", agent_callable=False),
    ActionSpec("send_message", R3, Message, "inbox", "POST", "/mock/lender/{merchant_id}/messages",
               ("channel", "body", "language", "case_id", "recipient"), contact=True),
]


def build_registry() -> ActionRegistry:
    return ActionRegistry(SPECS)
