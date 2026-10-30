"""Consumer pack policy (#89): the core policy engine (default-deny, no admin bypass) with this pack's own rules. No merchant-pack code."""
from __future__ import annotations

import re

from core.engine.policy import Finding, PolicyEngine, Rule

SRC = "Bible section 30 (consumer pack)"
_SECRET = re.compile(r"\b(otp|m?pin|cvv|password)\b\D{0,24}\d{4,8}|\d{4,8}\D{0,24}\b(otp|m?pin|cvv|password)\b", re.I)
_ASK = re.compile(r"\b(share|send|tell|give|enter|batao|bhejo)\b[^.!?\n]{0,30}\b(otp|m?pin|cvv|password)\b", re.I)
LIMITS = {"money_limit_paise": 1_000_000}


def _texts(args: dict) -> list[str]:
    return [v for v in args.values() if isinstance(v, str)]


def secret(ctx):
    for t in _texts(ctx.args):
        if _SECRET.search(t) or (_ASK.search(t) and not re.search(r"\b(never|don'?t|do not|mat|kabhi)\b", t, re.I)):
            return Finding("an OTP/PIN is requested or repeated")
    return None


def money_limit(ctx):
    """A dispute on a debit of Rs 10,000 or more goes to a person (the amount is read from the system, not taken from the args)."""
    if ctx.intent.principal.kind == "HUMAN" or ctx.intent.action != "raise_upi_dispute":
        return None
    row = ctx.conn.execute("SELECT amount_paise FROM consumer_txns WHERE txn_id = ?", (ctx.args.get("txn_id"),)).fetchone()
    if row and row["amount_paise"] >= ctx.limits["money_limit_paise"]:
        return Finding(f"{row['amount_paise']} paise at risk is at or above the {ctx.limits['money_limit_paise']} paise limit", route="support_executive")
    return None


def consent(ctx):
    """No message on a channel the consumer has not granted (core consent table)."""
    if ctx.intent.action != "send_message":
        return None
    ch = ctx.args.get("channel")
    row = ctx.conn.execute("SELECT status FROM consent WHERE subject_id = ? AND channel = ?", (ctx.args.get("merchant_id"), ch)).fetchone()
    return None if row and row["status"] == "GRANTED" else Finding(f"no GRANTED consent on channel {ch!r}")


RULES = (
    Rule("C-SECRET-001", "No PIN/OTP requested, stored or repeated", "BLOCK", SRC, secret),
    Rule("C-CONSENT-001", "No message on a channel without consent", "BLOCK", SRC, consent, {"actions": ["send_message"]}),
    Rule("C-MONEY-LIMIT-001", "A dispute on Rs 10,000 or more goes to a person", "ESCALATE", SRC, money_limit, {"actions": ["raise_upi_dispute"]}),
)


def build_engine(conn, clock, registry, *, playbook_dir=None, reader=None) -> PolicyEngine:
    return PolicyEngine(conn, clock, registry, RULES, LIMITS, playbook_dir=playbook_dir, reader=reader)
