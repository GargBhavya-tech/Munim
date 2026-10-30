"""The whitelisted field namespace that done-checks, step conditions and preconditions may read.

Every system field is derived from a live R0 read performed through the Reader on EVERY call (nothing here
caches). Case facts (touches, steps.<name>.done, ...) are passed in by the case engine.
"""
from __future__ import annotations

from typing import Any

from core.actions.types import Reader

# field -> the R0 action that supplies it (None = a case fact, supplied by the caller)
SYSTEM_FIELDS: dict[str, str] = {
    "kyc.status": "get_kyc_status",
    "kyc.reason": "get_kyc_status",
    "kyc.attempts": "get_kyc_status",
    "bank.status": "get_bank_status",
    "bank.reason": "get_bank_status",
    "bank.attempts": "get_bank_status",
    "device.status": "get_device_status",
    "device.replacements_last_30d": "get_device_status",
    "device.last_test_announcement_ok": "get_device_status",
    "device.remote_fix_attempts": "get_device_status",
    "payment.status": "get_transactions",
    "settlement.status": "get_settlements",
    "settlement.reason": "get_settlements",
    "settlement.amount_paise": "get_settlements",
    "payment.latest_amount_paise": "get_transactions",
    "device.order_status": "get_device_status",
    "device.replacement_status": "get_device_status",
    "plan.plan_id": "get_plan",
    "plan.status": "get_plan",
    "plan.pending_plan_id": "get_plan",
    "plan.billing_date": "get_plan",
    "plan.change_status": "get_plan",
    "refund.status": "get_refunds",
    "closure.status": "get_closures",
    "ticket.open_count": "get_tickets",
    "ticket.latest_status": "get_tickets",
    "risk.has_open_hold": "get_risk_status",
    "risk.open_flags": "get_risk_status",
    "lender.eligible": "check_partner_eligibility",
    "lender.max_amount_paise": "check_partner_eligibility",
    "lender.app_status": "get_lender_applications",
    "offer.latest_status": "get_offers",
}
# bare aliases used in the Bible's playbook examples
ALIASES: dict[str, str] = {
    "last_test_announcement_ok": "device.last_test_announcement_ok",
}
CASE_FACTS = frozenset({"touches", "intent", "replacements_last_30d", "language", "follow_up_pending"})

# Fields another pack adds (#89: packs are independent; the core must not know their reads). field -> (R0 read, extractor(body) -> value)
PACK_FIELDS: dict[str, tuple[str, Any]] = {}


def register_fields(fields: dict[str, tuple[str, Any]]) -> None:
    """Called by a pack at import: ``{"upi.status": ("get_upi_transactions", lambda body: ...)}``. A name the core already owns, or one
    registered with a different read, is an error (two packs must never silently redefine the same field)."""
    for name, (read, fn) in fields.items():
        if name in SYSTEM_FIELDS or name in CASE_FACTS:
            raise ValueError(f"field {name!r} belongs to the core namespace")
        if name in PACK_FIELDS and PACK_FIELDS[name][0] != read:
            raise ValueError(f"field {name!r} is already registered with read {PACK_FIELDS[name][0]!r}")
        PACK_FIELDS[name] = (read, fn)


def allowed_fields(step_names: list[str] | tuple[str, ...] = ()) -> set[str]:
    out = set(SYSTEM_FIELDS) | set(ALIASES) | set(CASE_FACTS) | set(PACK_FIELDS)
    out |= {f"steps.{n}.done" for n in step_names}
    return out


def read_for_field(field: str) -> str | None:
    """The R0 read a field needs, or None for case facts. replacements_last_30d prefers the case fact but
    falls back to the device read, so it needs no declared read."""
    field = ALIASES.get(field, field)
    return SYSTEM_FIELDS.get(field) or (PACK_FIELDS[field][0] if field in PACK_FIELDS else None)


def _flatten(prefix: str, value: Any, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out[prefix] = value


def build_namespace(reader: Reader, merchant_id: str, required_reads: list[str], case_facts: dict | None = None) -> dict:
    """Call the Reader for every required read (fresh, every time) and flatten into a dotted-name dict."""
    ns: dict[str, Any] = {}
    args = {"merchant_id": merchant_id}
    bodies: dict[str, Any] = {}
    for name in required_reads:
        body = bodies[name] = reader(name, dict(args))
        if name == "get_kyc_status":
            for k in ("status", "reason", "attempts"):
                ns[f"kyc.{k}"] = body.get(k)
        elif name == "get_bank_status":
            for k in ("status", "reason", "attempts"):
                ns[f"bank.{k}"] = body.get(k)
        elif name == "get_device_status":
            for k in ("status", "replacements_last_30d", "last_test_announcement_ok"):
                ns[f"device.{k}"] = body.get(k)
            ns["device.remote_fix_attempts"] = int(body.get("remote_fix_attempts") or 0)
            orders, reps = body.get("orders") or [], body.get("replacements") or []
            ns["device.order_status"] = orders[-1]["status"] if orders else "NONE"       # oldest first, so the last is the newest
            ns["device.replacement_status"] = reps[-1]["status"] if reps else "NONE"
            ns["device.last_test_announcement_ok"] = bool(body.get("last_test_announcement_ok"))
        elif name == "get_plan":
            ns["plan.plan_id"], ns["plan.status"] = body.get("plan_id"), body.get("status")
            ns["plan.pending_plan_id"], ns["plan.billing_date"] = body.get("pending_plan_id"), body.get("billing_date")
            ch = body.get("changes") or []
            ns["plan.change_status"] = ch[-1]["status"] if ch else "NONE"          # oldest first, so the last is the newest
        elif name == "get_refunds":
            rows = body.get("refunds") or []
            ns["refund.status"] = rows[-1]["status"] if rows else "NONE"
        elif name == "get_closures":
            rows = body.get("closures") or []
            ns["closure.status"] = rows[-1]["status"] if rows else "NONE"
        elif name == "get_tickets":
            rows = body.get("tickets") or []
            ns["ticket.open_count"] = sum(1 for t in rows if t["status"] == "OPEN")
            ns["ticket.latest_status"] = rows[-1]["status"] if rows else "NONE"
        elif name == "get_risk_status":
            ns["risk.has_open_hold"], ns["risk.open_flags"] = bool(body.get("has_open_hold")), body.get("open_flags", 0)
        elif name == "check_partner_eligibility":
            ns["lender.eligible"], ns["lender.max_amount_paise"] = bool(body.get("eligible")), body.get("max_amount_paise", 0)
        elif name == "get_lender_applications":
            rows = body.get("applications") or []
            ns["lender.app_status"] = rows[-1]["status"] if rows else "NONE"
        elif name == "get_offers":
            rows = body.get("waivers") or []
            ns["offer.latest_status"] = rows[-1]["status"] if rows else "NONE"
        elif name == "get_transactions":
            pays = body.get("payments") or []
            ns["payment.status"] = pays[0]["status"] if pays else "NONE"   # the list is newest first
            ns["payment.latest_amount_paise"] = pays[0]["amount_paise"] if pays else 0
            ns["_latest_payment"] = pays[0] if pays else None
        elif name == "get_settlements":
            ns["_settlements"] = body.get("settlements") or []
    if "_settlements" in ns:
        sets = ns["_settlements"]
        pay = ns.get("_latest_payment")
        linked = None
        if pay and pay.get("settlement_id"):
            linked = next((s for s in sets if s["settlement_id"] == pay["settlement_id"]), None)
        pick = linked or (sets[0] if sets else None)   # newest first
        ns["settlement.status"] = pick["status"] if pick else "NONE"
        ns["settlement.reason"] = pick.get("reason") if pick else None
        ns["settlement.amount_paise"] = pick["amount_paise"] if pick else 0
    ns.pop("_settlements", None)
    ns.pop("_latest_payment", None)
    for fname, (read, fn) in PACK_FIELDS.items():             # a pack's own fields, from the same fresh reads
        if read in bodies:
            ns[fname] = fn(bodies[read])
    facts: dict[str, Any] = {}
    _flatten("", case_facts or {}, facts)
    ns.update(facts)
    ns["replacements_last_30d"] = facts.get("replacements_last_30d", ns.get("device.replacements_last_30d"))
    ns["last_test_announcement_ok"] = ns.get("device.last_test_announcement_ok")
    return ns
