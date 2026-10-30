"""Agent-to-agent request to the lender's agent (Build Map #115): Munim's side of the protocol.

Munim sends the lender's agent a SIGNED request ``{merchant_token, consent_scope, purpose, ts}``. The lender's agent (a mock of an external system:
``GET /mock/lender/agent`` in packs/paytm_merchant/mocks/lender.py, the signed request in the query) checks the signature, the scope and the age, applies the LENDER's own eligibility
rule, and answers ``{eligible, max_amount_paise, kfs_ref}``. Munim never decides eligibility: when the lender does not answer the result is PENDING and
carries no eligibility field at all, so there is nothing for the merchant-facing text to quote. The merchant's data leaves only with a granted consent for
exactly this scope, and only as an opaque token.

The request goes through ``core.actions.executor`` (``ask_lender_agent``, an R0 the model cannot call): the ledger entry with exactly what is sent is
written BEFORE the request, the circuit breaker and chaos apply, and nothing here talks to a mock directly.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from core.engine.clock import SimClock, iso

SCOPE = "lender_eligibility"            # the consent channel that must be GRANTED before anything is sent
PURPOSES = ("eligibility_check",)
MAX_AGE = timedelta(minutes=5)
ACTION = "ask_lender_agent"
# Holding answers. hi-IN and hinglish NEED NATIVE REVIEW (CLAUDE.md rule 8): "लेंडर" is a loanword; a reviewer may prefer "ऋणदाता" or the partner's name.
SAY_PENDING = {"en-IN": "I'll check with the lender and come back to you.",
               "hinglish": "Main lender se check karke aapko wapas batata hoon.",
               "hi-IN": "मैं लेंडर से पूछकर आपको वापस बताता हूँ।"}
NO_ANSWER = ("UPSTREAM_UNAVAILABLE", "CIRCUIT_OPEN")      # executor errors that mean the lender did not answer


def merchant_token(secret: bytes, merchant_id: str) -> str:
    return "mt_" + hmac.new(secret, merchant_id.encode(), hashlib.sha256).hexdigest()[:20]


def _body_bytes(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(secret: bytes, body: dict) -> str:
    return hmac.new(secret, _body_bytes(body), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class LenderAnswer:
    status: str                              # DECIDED | PENDING | NO_CONSENT | REFUSED
    eligible: bool | None = None             # only ever the lender's answer; None unless status is DECIDED
    max_amount_paise: int | None = None
    kfs_ref: str | None = None
    reasons: tuple = ()
    say: str | None = None
    detail: str = ""


class LenderClient:
    """Munim's side. It relays; it never computes or guesses an eligibility. ``executor`` is core.actions.executor.Executor."""

    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, executor, secret: bytes):
        self.conn, self.clock, self.ledger, self.executor, self.secret = conn, clock, ledger, executor, secret

    def _consented(self, merchant_id: str) -> bool:
        r = self.conn.execute("SELECT status FROM consent WHERE subject_id = ? AND channel = ?", (merchant_id, SCOPE)).fetchone()
        return bool(r and r["status"] == "GRANTED")

    def check(self, merchant_id: str, language: str = "en-IN", purpose: str = "eligibility_check", case_id: str | None = None) -> LenderAnswer:
        if not self._consented(merchant_id):
            self._note(case_id, merchant_id, "NO_CONSENT")
            return LenderAnswer("NO_CONSENT", detail="the merchant has not consented to sharing for a lender check")
        body = {"merchant_token": merchant_token(self.secret, merchant_id), "consent_scope": SCOPE, "purpose": purpose, "ts": iso(self.clock.now())}
        out = self.executor.read(ACTION, {**body, "signature": sign(self.secret, body)}, case_id=case_id)
        if out.status != "OK":
            err = out.error or ""
            if err.startswith(NO_ANSWER):
                self._note(case_id, merchant_id, "PENDING")
                return LenderAnswer("PENDING", say=SAY_PENDING.get(language, SAY_PENDING["en-IN"]))
            self._note(case_id, merchant_id, f"REFUSED:{err}")
            return LenderAnswer("REFUSED", detail=err)
        r = out.result
        self._note(case_id, merchant_id, "DECIDED", kfs_ref=r["kfs_ref"], eligible=r["eligible"])
        return LenderAnswer("DECIDED", r["eligible"], r["max_amount_paise"], r["kfs_ref"], tuple(r["reasons"]))

    def _note(self, case_id, merchant_id: str, result: str, **extra) -> None:
        self.ledger.append(kind="STATE", actor="SYSTEM", case_id=case_id, payload={"lender_agent": result, "merchant_id": merchant_id, **extra})
