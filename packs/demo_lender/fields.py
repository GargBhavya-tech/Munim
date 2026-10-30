"""The lender pack's own fields for done-checks and preconditions (registered with the core at import)."""
from __future__ import annotations


from core.engine.fields import register_fields


def _dpd(b: dict) -> int:
    return int(b.get("days_past_due", 0))


FIELDS = {
    "loan.status": ("get_loan_account", lambda b: b.get("status", "NONE")),
    "loan.due_amount_paise": ("get_loan_account", lambda b: int(b.get("due_amount_paise", 0))),
    "loan.dpd": ("get_loan_account", _dpd),
    "loan.in_dispute": ("get_loan_account", lambda b: bool(b.get("in_dispute"))),
    "loan.ptp_status": ("get_loan_account", lambda b: b.get("ptp_status", "NONE")),
}
register_fields(FIELDS)
