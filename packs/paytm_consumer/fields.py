"""The consumer pack's own fields for done-checks and preconditions (registered with the core at import; #89)."""
from __future__ import annotations

from core.engine.fields import register_fields


def _stuck(body: dict) -> dict | None:
    """The latest debit that left the consumer's account without reaching the payee (or the latest transaction)."""
    txns = body.get("transactions") or []
    pending = [t for t in txns if t["status"] in ("DEBITED_PENDING", "REVERSED")]
    return pending[0] if pending else (txns[0] if txns else None)


def _dispute(body: dict) -> str:
    t = _stuck(body)
    if not t:
        return "NONE"
    ds = [d for d in body.get("disputes") or [] if d["txn_id"] == t["txn_id"] and d["status"] != "WITHDRAWN"]
    return ds[0]["status"] if ds else "NONE"


def _refund(body: dict) -> dict | None:
    rs = body.get("refunds") or []
    return rs[0] if rs else None


FIELDS = {
    "upi.status": ("get_upi_transactions", lambda b: (_stuck(b) or {}).get("status", "NONE")),
    "upi.amount_paise": ("get_upi_transactions", lambda b: (_stuck(b) or {}).get("amount_paise", 0)),
    "upi.dispute_status": ("get_upi_transactions", _dispute),
    "crefund.status": ("get_consumer_refunds", lambda b: (_refund(b) or {}).get("status", "NONE")),
    "crefund.overdue": ("get_consumer_refunds", lambda b: bool((_refund(b) or {}).get("overdue"))),
}
register_fields(FIELDS)
