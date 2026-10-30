"""Consented first contact (#102; Bible sections 11 and 12): the only way a new-merchant lead is first reached.

* WHO: only leads dedupe cleared (``jobs.dedupe.outreach_ready`` is the ONLY query that supplies NEW leads; a lead already moved on to
  RESEARCHED / NURTURE / NO_REPLY must still carry the CLEAR mark and no matched merchant).
* WHICH CHANNEL: by segment, from the channels the lead has a GRANTED consent row for. D2C -> email. Offline retail -> a field visit (high
  value only), then WhatsApp, then an AI voice call from the registered 140-series number. A channel without consent is never used and never
  guessed; a lead with no consented channel is left alone (``NO_CONSENTED_CHANNEL``) for a person to obtain consent.
* DND: the lead's public number is checked against ``dnd_registry`` on EVERY channel, business numbers included ("it is B2B" is not an
  exemption). Checked here first and again by the policy engine (CONSENT-001) because the send goes through the executor.
* WORDS: built only from the approved fixed phrases (packs/paytm_merchant/phrases.yaml): greeting, identity (names Munim and says it is an AI
  assistant) and "Paytm never asks for your PIN or OTP". No link of any kind is ever put in a cold message; ``check_first_contact`` refuses a
  body that lacks the lines or carries a link.
* HOW: ``send_message`` through ``core.actions.executor`` (policy: consent, DND, contact budget and quiet hours, default-deny; ledger before
  the side effect; deterministic idempotency key). This module never calls a mock. The key's ``step_seq`` is the lead's count of earlier
  proactive touches, so a re-run of the same first contact replays instead of sending twice, while a genuine retry later is a new send.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from core.actions.types import ActionIntent, Principal
from core.engine.clock import SimClock
from core.gate.phrases import Phrases, canon
from jobs.dedupe import outreach_ready
from jobs.lead_lifecycle import LeadLifecycle

OUTREACH_PLAYBOOK = "LEAD_OUTREACH"
PRINCIPAL = Principal("AGENT", "munim")
FIRST_CONTACT_PHRASES = ("greeting", "identity", "pin_otp")       # fixed approved text; order matters (identity before anything else is said)
REQUIRED_PHRASES = ("identity", "pin_otp")
SEGMENT_CHANNELS = {                                              # preference order within the lead's CONSENTED channels (Bible section 11)
    "d2c": ("email",),
    "offline": ("whatsapp", "voice"),
}
FIELD_VISIT = "field_visit"                                       # not a message: a person goes. Only for high-value offline leads.
HIGH_VALUE_SCORE = 6                                              # same cut as jobs.scoring.channel_for (field_rep at >= 6)
MESSAGE_CHANNELS = ("whatsapp", "voice", "email", "sms", "app", "soundbox")
CONTACTABLE_STATES = frozenset({"NEW", "RESEARCHED", "NURTURE", "NO_REPLY"})
D2C_CATEGORIES = frozenset({"d2c"})
_LINK = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|co|me|app|link|io|org|net)\b|\bbit\.ly\b|\bt\.me\b|\bpaytm\.me\b|\.apk\b)", re.I)

SENT, BLOCKED, NOT_ELIGIBLE, NO_CHANNEL, FIELD_ROUTED, SEND_FAILED = "SENT", "BLOCKED", "NOT_ELIGIBLE", "NO_CONSENTED_CHANNEL", "ROUTED_TO_FIELD", "SEND_FAILED"


class FirstContactInvalid(ValueError):
    """The message would go out without the identity / PIN-OTP lines, or with a link."""


def segment_for(category: str | None) -> str:
    return "d2c" if (category or "").lower() in D2C_CATEGORIES else "offline"


def contains_link(text: str) -> bool:
    return bool(_LINK.search(text or ""))


def check_first_contact(body: str, language: str, phrases: Phrases | None = None) -> list[str]:
    """Problems with a first-contact body (empty list = fine): a missing required line, or a link."""
    ph = phrases or Phrases()
    blob = canon(body)
    problems = [f"missing:{pid}" for pid in REQUIRED_PHRASES if canon(ph.text(pid, language)) not in blob]
    if contains_link(body):
        problems.append("link_in_cold_message")
    return problems


def build_first_contact(language: str, phrases: Phrases | None = None) -> str:
    ph = phrases or Phrases()
    body = " ".join(ph.text(pid, language) for pid in FIRST_CONTACT_PHRASES)
    problems = check_first_contact(body, language, ph)
    if problems:
        raise FirstContactInvalid(", ".join(problems))
    return body


@dataclass
class OutreachResult:
    lead_id: str
    status: str
    channel: str | None = None
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    action_id: str | None = None
    body: str | None = None


class OutreachJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, executor, lifecycle: LeadLifecycle, budget=None, phrases: Phrases | None = None,
                 after_contact=None):
        self.conn, self.clock, self.ledger, self.executor, self.life, self.budget = conn, clock, ledger, executor, lifecycle, budget
        self.phrases = phrases or Phrases()
        self.after_contact = after_contact                # e.g. ReplyHandler.arm_no_reply: sets the NO_REPLY timer once a contact really went out

    # ------------------------------------------------------------------------------------------------------ reads
    def _lead(self, lead_id: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if r is None:
            raise KeyError(lead_id)
        return r

    def eligible(self, row: sqlite3.Row) -> str | None:
        """None when the lead may be pitched, else the reason it may not."""
        if row["state"] not in CONTACTABLE_STATES:
            return f"state {row['state']} is not contactable"
        if row["matched_merchant_id"] is not None:
            return "already a Paytm merchant"
        if '"result": "CLEAR"' not in (row["provenance_json"] or ""):
            return "not cleared by duplicate matching"
        if row["state"] == "NEW" and row["lead_id"] not in {r["lead_id"] for r in outreach_ready(self.conn)}:
            return "not in the outreach-ready set"
        return None

    def in_dnd(self, row: sqlite3.Row) -> bool:
        phone = row["phone_public"]
        return bool(phone) and self.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (phone,)).fetchone() is not None

    def consented(self, lead_id: str) -> set[str]:
        return {r["channel"] for r in self.conn.execute("SELECT channel FROM consent WHERE subject_id = ? AND status = 'GRANTED'", (lead_id,))}

    def choose_channel(self, row: sqlite3.Row) -> str | None:
        have = self.consented(row["lead_id"])
        seg = segment_for(row["category"])
        order = SEGMENT_CHANNELS[seg]
        if seg == "offline" and (row["score"] or 0) >= HIGH_VALUE_SCORE:
            order = (FIELD_VISIT, *order)
        return next((c for c in order if c in have), None)

    def _touches(self, lead_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM contact_touches WHERE subject_id = ? AND direction = 'OUT'", (lead_id,)).fetchone()[0]

    # ------------------------------------------------------------------------------------------------------ the contact
    def first_contact(self, lead_id: str, language: str = "en-IN") -> OutreachResult:
        row = self._lead(lead_id)
        why = self.eligible(row)
        if why:
            return OutreachResult(lead_id, NOT_ELIGIBLE, reasons=[why])
        if self.in_dnd(row):
            self._note(lead_id, "DND", None)
            return OutreachResult(lead_id, BLOCKED, reasons=["DND: the number is in the do-not-disturb registry"], rule_ids=["DND"])
        channel = self.choose_channel(row)
        if channel is None:
            return OutreachResult(lead_id, NO_CHANNEL, reasons=["no consented channel for this segment"])
        if channel == FIELD_VISIT:
            self._note(lead_id, "FIELD_VISIT_ROUTED", channel)
            self._advance(row)
            return OutreachResult(lead_id, FIELD_ROUTED, channel=channel, reasons=["high-value offline lead: a person visits; no message is sent"])
        body = build_first_contact(language, self.phrases)
        intent = ActionIntent("send_message", {"merchant_id": lead_id, "channel": channel, "body": body, "language": language},
                              step_seq=self._touches(lead_id), principal=PRINCIPAL, playbook_id=OUTREACH_PLAYBOOK, step="first_contact",
                              proactive=True, channel=channel, allowlist=("send_message",), facts={"lead_id": lead_id})
        out = self.executor.submit(intent)
        if out.status in ("REFUSED", "ESCALATE"):
            return OutreachResult(lead_id, BLOCKED, channel, [out.error or out.status], [v.rule_id for v in out.violations], body=body)
        if out.status == "ERROR":
            return OutreachResult(lead_id, SEND_FAILED, channel, [out.error or "ERROR"], action_id=out.action_id, body=body)
        if out.status == "OK":                                        # a REPLAYED result was already counted and advanced the first time
            if self.budget is not None:
                self.budget.record(lead_id, channel, case_id=None)
            self._advance(row)
            if self.after_contact:
                self.after_contact(lead_id, language)
        return OutreachResult(lead_id, SENT, channel, action_id=out.action_id, body=body)

    def _advance(self, row: sqlite3.Row) -> None:
        state = row["state"]
        if state == "NEW":
            self.life.move(row["lead_id"], "RESEARCHED", "brief ready")
            state = "RESEARCHED"
        if state in ("RESEARCHED", "NURTURE", "NO_REPLY"):
            self.life.move(row["lead_id"], "CONTACTED", "first contact" if state == "RESEARCHED" else "retry")

    def _note(self, lead_id: str, what: str, channel: str | None) -> None:
        self.ledger.append(kind="STATE", actor="SYSTEM", payload={"job": "outreach", "lead_id": lead_id, "result": what, "channel": channel})

    def run(self, language: str = "en-IN", limit: int | None = None) -> list[OutreachResult]:
        """First contact for every outreach-ready lead (best score first)."""
        rows = sorted(outreach_ready(self.conn), key=lambda r: (-(r["score"] or 0), r["lead_id"]))
        return [self.first_contact(r["lead_id"], language) for r in rows[:limit]]


__all__ = ["OutreachJob", "OutreachResult", "FirstContactInvalid", "build_first_contact", "check_first_contact", "contains_link", "segment_for",
           "SENT", "BLOCKED", "NOT_ELIGIBLE", "NO_CHANNEL", "FIELD_ROUTED", "SEND_FAILED"]
