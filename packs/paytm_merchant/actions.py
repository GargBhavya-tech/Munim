"""The Paytm merchant pack's action vocabulary. Playbooks may only name actions that appear here."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass

R0, DRAFT, R1, R2, R3 = RiskClass.R0, RiskClass.DRAFT, RiskClass.R1, RiskClass.R2, RiskClass.R3


class Merchant(BaseModel):
    merchant_id: str


class Transactions(Merchant):
    limit: int = Field(default=20, ge=1, le=200)


class Ticket(Merchant):
    type: str
    system_object: Optional[str] = None
    summary: Optional[str] = None
    case_id: Optional[str] = None


class TicketRef(BaseModel):
    ticket_id: str


class FollowUp(Merchant):
    due_at: str
    case_id: Optional[str] = None
    note: Optional[str] = None
    kind: str = "follow_up"


class ScheduleRef(BaseModel):
    schedule_id: str


class Booking(Merchant):
    kind: str = Field(pattern="^(visit|call)$")
    slot_at: str
    person_id: Optional[str] = None


class BookingRef(BaseModel):
    booking_id: str


class Replacement(Merchant):
    reason: str = "UNSPECIFIED"


class ReplacementRef(Merchant):
    replacement_id: str


class Order(Merchant):
    model: str = "Soundbox 4G"


class OrderRef(Merchant):
    order_id: str


class Waiver(Merchant):
    months: int = Field(gt=0)
    offer_id: str = "FEE_WAIVER_1M"


class WaiverRef(BaseModel):
    waiver_id: str


class Refund(Merchant):
    payment_id: str
    amount_paise: int = Field(gt=0)


class RefundRef(BaseModel):
    refund_id: str


class Closure(Merchant):
    reason: Optional[str] = None


class ClosureRef(BaseModel):
    closure_id: str


class PlanChange(Merchant):
    to_plan_id: str


class PlanChangeRef(Merchant):
    change_id: str


class Message(BaseModel):
    """A message to a merchant (merchant_id) or to a lead (lead_id, its own id namespace). Exactly one recipient is named. Consent and DND for a
    lead are checked against the leads table by CONSENT-001; nothing about the rules is relaxed for leads."""
    merchant_id: Optional[str] = None
    lead_id: Optional[str] = None
    channel: str
    body: str
    language: Optional[str] = None
    case_id: Optional[str] = None

    @model_validator(mode="after")
    def _one_recipient(self):
        if bool(self.merchant_id) == bool(self.lead_id):
            raise ValueError("name exactly one recipient: merchant_id or lead_id")
        return self


class FieldNote(BaseModel):
    """An internal message to a rep or technician (a staff member, not a merchant or lead)."""
    person_id: str = Field(min_length=1, max_length=40)
    body: str = Field(min_length=1)
    language: Optional[str] = None
    subject: Optional[str] = None


class Preference(Merchant):
    key: str
    value: str


class SettlementRef(BaseModel):
    settlement_id: str


class Draft(Merchant):
    text: str


class DeviceConfig(Merchant):
    config_version: str = Field(default="cfg-latest", max_length=40)


class RemoteOpRef(Merchant):
    op_id: str


class PaymentRef(Merchant):
    reference: str = Field(min_length=4, max_length=40)


class LenderAsk(BaseModel):
    """The signed request to the lender's agent: an opaque merchant token, never the merchant id."""
    merchant_token: str = Field(pattern=r"^mt_[0-9a-f]{20}$")
    consent_scope: str
    purpose: str
    ts: str
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class FraudFlag(Merchant):
    kind: str = Field(pattern="^(FAKE_PAYMENT_SCREENSHOT|FRAUD_CALL|REFERENCE_MISMATCH)$")
    detail: Optional[str] = None


def _id(field: str, *carry: str):
    """comp_args builder: the compensation takes the created object's id (plus any carried args)."""
    return lambda args, result: {**{k: args[k] for k in carry}, field: result[field]}


SPECS = [
    # ---- R0 reads -------------------------------------------------------------------------------------
    ActionSpec("get_merchant", R0, Merchant, "merchant", "GET", "/mock/merchants/{merchant_id}"),
    ActionSpec("get_kyc_status", R0, Merchant, "kyc", "GET", "/mock/kyc/{merchant_id}"),
    ActionSpec("get_bank_status", R0, Merchant, "bank", "GET", "/mock/bank/{merchant_id}"),
    ActionSpec("get_device_status", R0, Merchant, "device", "GET", "/mock/devices/{merchant_id}"),
    ActionSpec("get_transactions", R0, Transactions, "payments", "GET", "/mock/payments?mid={merchant_id}&limit={limit}"),
    ActionSpec("get_settlements", R0, Merchant, "settlement", "GET", "/mock/settlements?mid={merchant_id}"),
    ActionSpec("get_offer_band", R0, Merchant, "pricing", "GET", "/mock/offers/band?mid={merchant_id}"),
    ActionSpec("get_plan", R0, Merchant, "plans", "GET", "/mock/plans/{merchant_id}"),
    ActionSpec("get_risk_status", R0, Merchant, "risk", "GET", "/mock/risk/{merchant_id}"),
    ActionSpec("get_tickets", R0, Merchant, "tickets", "GET", "/mock/tickets?mid={merchant_id}"),
    ActionSpec("get_refunds", R0, Merchant, "refunds", "GET", "/mock/refunds?mid={merchant_id}"),
    ActionSpec("get_schedule", R0, Merchant, "scheduler", "GET", "/mock/schedule?mid={merchant_id}"),
    ActionSpec("get_closures", R0, Merchant, "closures", "GET", "/mock/closures?mid={merchant_id}"),
    ActionSpec("get_offers", R0, Merchant, "pricing", "GET", "/mock/offers?mid={merchant_id}"),
    ActionSpec("get_lender_applications", R0, Merchant, "lender", "GET", "/mock/lender/applications?mid={merchant_id}"),
    ActionSpec("check_partner_eligibility", R0, Merchant, "lender", "GET", "/mock/lender/eligibility?mid={merchant_id}"),
    # The signed agent-to-agent request (#115). agent_callable=False: only integrations/lender_agent.LenderClient sends it, after the consent check.
    ActionSpec("ask_lender_agent", R0, LenderAsk, "lender", "GET",
               "/mock/lender/agent?merchant_token={merchant_token}&consent_scope={consent_scope}&purpose={purpose}&ts={ts}&signature={signature}",
               agent_callable=False),
    ActionSpec("check_device_connectivity", R0, Merchant, "device", "GET", "/mock/devices/{merchant_id}/connectivity"),
    ActionSpec("get_payment_by_reference", R0, PaymentRef, "payments", "GET", "/mock/payments/by-reference?mid={merchant_id}&ref={reference}"),
    # ---- DRAFT: prepare, never commit ---------------------------------------------------------------
    ActionSpec("draft_message", DRAFT, Draft),
    ActionSpec("draft_ticket", DRAFT, Ticket),
    # ---- R1 reversible ----------------------------------------------------------------------------------
    ActionSpec("schedule_follow_up", R1, FollowUp, "scheduler", "POST", "/mock/schedule", ("merchant_id", "due_at", "case_id", "note", "kind"),
               compensation="cancel_follow_up", comp_args=_id("schedule_id")),
    ActionSpec("book_visit_or_call", R1, Booking, "scheduler", "POST", "/mock/bookings", ("merchant_id", "kind", "slot_at", "person_id"),
               compensation="cancel_booking", comp_args=_id("booking_id")),
    ActionSpec("save_preference", R1, Preference, compensation="restore_preference",
               comp_args=lambda a, r: {"merchant_id": a["merchant_id"], "key": a["key"], "value": r.get("previous_value") or ""}),
    # ---- R2 compensable within a window -------------------------------------------------------------
    ActionSpec("raise_ticket", R2, Ticket, "tickets", "POST", "/mock/tickets", ("merchant_id", "type", "system_object", "summary", "case_id"),
               compensation="withdraw_ticket", comp_args=_id("ticket_id")),
    ActionSpec("create_device_replacement", R2, Replacement, "device", "POST", "/mock/devices/{merchant_id}/replacement", ("reason",),
               compensation="cancel_device_replacement", window_hours=24, comp_args=_id("replacement_id", "merchant_id")),
    ActionSpec("create_device_order", R2, Order, "device", "POST", "/mock/devices/{merchant_id}/order", ("model",),
               compensation="cancel_device_order", window_hours=24, comp_args=_id("order_id", "merchant_id"), offer=True),
    ActionSpec("apply_fee_waiver", R2, Waiver, "pricing", "POST", "/mock/offers/waiver", ("merchant_id", "months", "offer_id"),
               compensation="revoke_fee_waiver", window_hours=24 * 7, comp_args=_id("waiver_id"), offer=True),
    ActionSpec("create_refund_request", R2, Refund, "refunds", "POST", "/mock/refunds", ("merchant_id", "payment_id", "amount_paise"),
               compensation="cancel_refund_request", window_hours=24, comp_args=_id("refund_id")),
    ActionSpec("create_closure_request", R2, Closure, "closures", "POST", "/mock/closures", ("merchant_id", "reason"),
               compensation="cancel_closure_request", window_hours=48, comp_args=_id("closure_id")),
    ActionSpec("create_plan_change", R2, PlanChange, "plans", "POST", "/mock/plans/{merchant_id}", ("to_plan_id",),
               compensation="revert_plan_change", comp_args=_id("change_id", "merchant_id"), offer=True),
    # ---- R3 irreversible: drafted, checked, outboxed, sent ---------------------------------------------
    ActionSpec("send_message", R3, Message, "comms", "POST", "/mock/comms/send", ("merchant_id", "lead_id", "channel", "body", "language", "case_id"), contact=True),
    ActionSpec("notify_field_person", R3, FieldNote, "comms", "POST", "/mock/comms/notify-staff", ("person_id", "body", "language", "subject"),
               description="Internal note to a rep or technician; not a merchant contact (no consent or contact budget)"),
    ActionSpec("send_kyc_resume_link", R3, Merchant, "kyc", "POST", "/mock/kyc/{merchant_id}/resume-link", contact=True),
    ActionSpec("send_bank_update_link", R3, Merchant, "bank", "POST", "/mock/bank/{merchant_id}/update-link", contact=True),
    ActionSpec("send_partner_application_link", R3, Merchant, "lender", "POST", "/mock/lender/application-link", ("merchant_id",), contact=True, offer=True),
    ActionSpec("retrigger_bank_verification", R3, Merchant, "bank", "POST", "/mock/bank/{merchant_id}/retrigger"),
    # remote-fix toolkit (#93). Firmware updates are ops-only and deliberately NOT registered: no agent can ever call one.
    ActionSpec("restart_device", R3, Merchant, "device", "POST", "/mock/devices/{merchant_id}/restart",
               description="Remote reboot of the merchant's Soundbox"),
    ActionSpec("reset_device_volume", R3, Merchant, "device", "POST", "/mock/devices/{merchant_id}/volume-reset",
               description="Reset the Soundbox volume to the audible default"),
    ActionSpec("push_device_config", R2, DeviceConfig, "device", "POST", "/mock/devices/{merchant_id}/config", ("config_version",),
               compensation="rollback_device_config", window_hours=24, comp_args=_id("op_id", "merchant_id"),
               description="Push the known-good configuration (rollback within 24 h)"),
    ActionSpec("rollback_device_config", R1, RemoteOpRef, "device", "DELETE", "/mock/devices/{merchant_id}/config/{op_id}", agent_callable=False),
    ActionSpec("flag_fraud", R3, FraudFlag, "risk", "POST", "/mock/risk/{merchant_id}/flags", ("kind", "detail"),
               description="Scam Shield: raise a fraud signal for the risk team; a reviewer clears it"),
    # ---- compensations: run by the executor/Undo, never offered to the model ------------------------------
    ActionSpec("cancel_follow_up", R1, ScheduleRef, "scheduler", "DELETE", "/mock/schedule/{schedule_id}", agent_callable=False),
    ActionSpec("cancel_booking", R1, BookingRef, "scheduler", "DELETE", "/mock/bookings/{booking_id}", agent_callable=False),
    ActionSpec("restore_preference", R1, Preference, agent_callable=False),
    ActionSpec("withdraw_ticket", R2, TicketRef, "tickets", "DELETE", "/mock/tickets/{ticket_id}", agent_callable=False),
    ActionSpec("cancel_device_replacement", R2, ReplacementRef, "device", "DELETE", "/mock/devices/{merchant_id}/replacement/{replacement_id}", agent_callable=False),
    ActionSpec("cancel_device_order", R2, OrderRef, "device", "DELETE", "/mock/devices/{merchant_id}/order/{order_id}", agent_callable=False),
    ActionSpec("revoke_fee_waiver", R2, WaiverRef, "pricing", "DELETE", "/mock/offers/waiver/{waiver_id}", agent_callable=False),
    ActionSpec("cancel_refund_request", R2, RefundRef, "refunds", "DELETE", "/mock/refunds/{refund_id}", agent_callable=False),
    ActionSpec("cancel_closure_request", R2, ClosureRef, "closures", "DELETE", "/mock/closures/{closure_id}", agent_callable=False),
    ActionSpec("revert_plan_change", R2, PlanChangeRef, "plans", "DELETE", "/mock/plans/{merchant_id}/change/{change_id}", agent_callable=False),
    # ---- human-only: a risk reviewer clears a hold; the agent never has this ----------------------------------
    ActionSpec("clear_risk_hold", R3, SettlementRef, "settlement", "POST", "/mock/settlements/{settlement_id}/advance", ("settlement_id",), agent_callable=False,
               description="Body is {'to': 'SETTLED'}; role-gated by ROLE-001/RISK-HOLD-001"),
]

def build_registry() -> ActionRegistry:
    return ActionRegistry(SPECS)
