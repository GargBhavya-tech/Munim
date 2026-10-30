"""Lender-collections policy: the core policy engine (default-deny, no admin bypass) with this pack's own rules. No other pack's code.

Rules: no threats or harassment, contact windows, no disclosure to third parties, consent on the channel, a promise-to-pay only with the
borrower's own consent, no PIN/OTP, and every rupee amount in a message must be the system's own figure. Rule source documents are
SIMULATED internal collections-conduct guidelines for the demo lender; the 09:00-19:00 window is a configured limit, not a legal claim.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from core.engine.policy import Finding, PolicyEngine, Rule

SRC = "Demo lender collections conduct guidelines (SIMULATED) / Bible section 36"
LIMITS = {"contact_window_start_h": 9, "contact_window_end_h": 19, "max_promise_days": 14}

_THREAT = re.compile(
    r"\b(police|arrest|jail|court|lawyer|legal action|case file|fir|sue|seize|confiscate|blacklist|cibil\s+destroy|ruin|"
    r"your family|your employer|your boss|neighbou?rs?|shame|disgrace|public|ghar\s+aa|gunde|dhamki|badnaam|izzat|"
    r"pay now or|pay immediately or|last warning|or else|warna|varna)\b|पुलिस|गिरफ्तार|बदनाम|धमकी", re.I)
_SECRET = re.compile(r"\b(otp|m?pin|cvv|password)\b\D{0,24}\d{4,8}|\d{4,8}\D{0,24}\b(otp|m?pin|cvv|password)\b", re.I)
_ASK = re.compile(r"\b(share|send|tell|give|enter|batao|bhejo)\b[^.!?\n]{0,30}\b(otp|m?pin|cvv|password)\b", re.I)
_MONEY = re.compile(r"(?:₹|\brs\.?|\binr)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_OTHER_ID = re.compile(r"\bB-\d{4}\b")


def _texts(args: dict) -> list[str]:
    return [v for v in args.values() if isinstance(v, str)]


def tone(ctx):
    for t in _texts(ctx.args):
        m = _THREAT.search(t)
        if m:
            return Finding(f"threatening or shaming language ({m.group(0)!r})")
    return None


def contact_window(ctx):
    """A message Munim starts (not a reply to the borrower) only inside the contact window, read from the injected clock."""
    if not ctx.intent.proactive:
        return None
    h = ctx.engine.clock.now().hour
    lo, hi = ctx.limits["contact_window_start_h"], ctx.limits["contact_window_end_h"]
    return None if lo <= h < hi else Finding(f"proactive contact at hour {h} is outside the {lo}:00-{hi}:00 window")


def privacy(ctx):
    """Only the borrower is ever told about the loan: no other recipient, and no other borrower's id or name in the text."""
    if ctx.args.get("recipient", "borrower") != "borrower":
        return Finding(f"recipient {ctx.args.get('recipient')!r} is a third party")
    me = ctx.args.get("merchant_id")
    body = ctx.args.get("body", "")
    for bid in _OTHER_ID.findall(body):
        if bid != me:
            return Finding(f"the message names another borrower ({bid})")
    for (name,) in ctx.conn.execute("SELECT name FROM borrowers WHERE borrower_id != ?", (me,)):
        if name.lower() in body.lower():
            return Finding("the message names another borrower")
    return None


def consent(ctx):
    ch = ctx.args.get("channel")
    row = ctx.conn.execute("SELECT status FROM consent WHERE subject_id = ? AND channel = ?", (ctx.args.get("merchant_id"), ch)).fetchone()
    return None if row and row["status"] == "GRANTED" else Finding(f"no GRANTED consent on channel {ch!r}")


def secret(ctx):
    for t in _texts(ctx.args):
        if _SECRET.search(t) or (_ASK.search(t) and not re.search(r"\b(never|don'?t|do not|mat|kabhi)\b", t, re.I)):
            return Finding("an OTP/PIN is requested or repeated")
    return None


def amounts(ctx):
    """Every rupee figure in a message must equal the amount the loan system returns now. Fails closed if the read fails."""
    body = ctx.args.get("body", "")
    found = _MONEY.findall(body)
    if not found:
        return None
    acct = ctx.read("get_loan_account", {"merchant_id": ctx.args.get("merchant_id")})
    if acct is None:
        return Finding("an amount is quoted but the account could not be read")
    for raw in found:
        paise = int((Decimal(raw.replace(",", "")) * 100).to_integral_value())
        if paise != acct["due_amount_paise"]:
            return Finding(f"the message quotes {paise} paise; the system says {acct['due_amount_paise']}")
    return None


def promise_consent(ctx):
    return None if ctx.args.get("borrower_consented") is True else Finding("the borrower has not said, themselves, that they promise to pay")


def promise_date(ctx):
    """A promise date in the past, or further out than the limit, is for a person to agree (the date maths is code, not the model)."""
    try:
        d = date.fromisoformat(ctx.args.get("promised_on", ""))
    except ValueError:
        return Finding("the promise date is not a date")
    days = (d - ctx.engine.clock.now().date()).days
    if 0 <= days <= ctx.limits["max_promise_days"]:
        return None
    return Finding(f"a promise {days} days from today is outside 0-{ctx.limits['max_promise_days']}", route="collections_officer")


MSG = {"actions": ["send_message"]}
RULES = (
    Rule("D-TONE-001", "No threats, shaming or harassment", "BLOCK", SRC, tone, MSG),
    Rule("D-WINDOW-001", "Proactive contact only inside the contact window", "BLOCK", SRC, contact_window, MSG),
    Rule("D-PRIVACY-001", "Nothing about a loan goes to a third party", "BLOCK", SRC, privacy, MSG),
    Rule("D-CONSENT-001", "No message on a channel without consent", "BLOCK", SRC, consent, MSG),
    Rule("D-SECRET-001", "No PIN/OTP requested, stored or repeated", "BLOCK", SRC, secret, MSG),
    Rule("D-NUMBERS-001", "Quoted amounts equal the system's amount", "BLOCK", SRC, amounts, MSG),
    Rule("D-PTP-001", "A promise-to-pay needs the borrower's own consent", "BLOCK", SRC, promise_consent, {"actions": ["record_promise_to_pay"]}),
    Rule("D-PTP-002", "A promise date is today up to the limit", "ESCALATE", SRC, promise_date, {"actions": ["record_promise_to_pay"]}),
)


def build_engine(conn, clock, registry, *, playbook_dir=None, reader=None) -> PolicyEngine:
    return PolicyEngine(conn, clock, registry, RULES, LIMITS, playbook_dir=playbook_dir, reader=reader)
