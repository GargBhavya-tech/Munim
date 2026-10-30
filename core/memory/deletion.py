"""Consent withdrawal and "delete my data" (Bible sections 33 and 37).

Deleted: profile facts, episode summaries, CRM entries written for the merchant, and the text of stored transcripts
(``turns.text_redacted`` is overwritten with a marker, ``decision_json`` cleared, ``cases.resume_note`` cleared).

KEPT (``LEGALLY_REQUIRED``), because the law or the audit trail requires it, never touched here:
  ledger            tamper-evident hash chain (also protected by DB triggers; altering any row breaks verify_chain)
  events            append-only operational event log (DB triggers); referenced by the ledger
  actions, compensations, idempotency, approvals, handoffs   financial / regulated action records (RBI audit period)
  payments, settlements, refunds, closures, mandates, lender_apps, plan_changes   transaction and KYC-linked records
  consent, contact_touches   proof that a stop / withdrawal was honoured (DPDP consent record, DND/TRAI compliance)
  cases (row skeleton)   case id, playbook, state, timestamps; free text removed
The merchant's consent for memory is then marked REVOKED so memory is not rebuilt.
"""
from __future__ import annotations

import sqlite3

from core.audit.ledger import Ledger
from core.engine.clock import SimClock
from core.memory.profile import MEMORY_SCOPE

LEGALLY_REQUIRED = {
    "ledger": "append-only hash-chained audit trail (audit period)",
    "events": "append-only operational event log referenced by the ledger",
    "actions": "record of money/regulated actions taken (RBI audit period)",
    "compensations": "reversal records for actions taken",
    "approvals": "human approval evidence for regulated actions",
    "handoffs": "human handoff records",
    "payments": "payment records (RBI payment data rules)",
    "settlements": "settlement records (RBI payment data rules)",
    "refunds": "refund records (RBI refund rules)",
    "mandates": "mandate records",
    "lender_apps": "loan application records (digital lending directions)",
    "consent": "evidence that a stop/withdrawal was recorded and honoured",
    "contact_touches": "contact log needed to prove DND / contact-budget compliance",
    "cases": "case skeleton (id, playbook, state, timestamps) for the audit trail; free text removed",
}
REDACTED = "[deleted on merchant request]"


def withdraw_consent(conn: sqlite3.Connection, clock: SimClock, merchant_id: str, scope: str = MEMORY_SCOPE,
                     source: str = "merchant_withdrawal") -> None:
    """Revoke ``scope`` (a channel, '*' or 'memory'). Memory use for that scope stops immediately; nothing is deleted."""
    conn.execute(
        "INSERT INTO consent(subject_id, channel, status, source, ts) VALUES (?,?, 'REVOKED', ?, ?) "
        "ON CONFLICT(subject_id, channel) DO UPDATE SET status='REVOKED', source=excluded.source, ts=excluded.ts",
        (merchant_id, scope, source, clock.now_iso()))


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def delete_merchant_memory(conn: sqlite3.Connection, clock: SimClock, merchant_id: str, ledger: Ledger | None = None,
                           requested_by: str = "merchant") -> dict[str, int]:
    """Delete merchant-level memory; never touches the ledger. Returns row counts per table."""
    counts: dict[str, int] = {}
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        counts["profile_facts"] = conn.execute("DELETE FROM profile_facts WHERE merchant_id = ?", (merchant_id,)).rowcount
        counts["episodes"] = conn.execute("DELETE FROM episodes WHERE merchant_id = ?", (merchant_id,)).rowcount
        if _has_table(conn, "crm_fields"):
            counts["crm_fields"] = conn.execute("DELETE FROM crm_fields WHERE merchant_id = ?", (merchant_id,)).rowcount
            counts["crm_log"] = conn.execute("DELETE FROM crm_log WHERE merchant_id = ?", (merchant_id,)).rowcount
        counts["turns_redacted"] = conn.execute(
            "UPDATE turns SET text_redacted = ?, decision_json = NULL WHERE case_id IN (SELECT case_id FROM cases WHERE merchant_id = ?)",
            (REDACTED, merchant_id)).rowcount
        counts["case_notes_cleared"] = conn.execute(
            "UPDATE cases SET resume_note = NULL WHERE merchant_id = ? AND resume_note IS NOT NULL", (merchant_id,)).rowcount
        withdraw_consent(conn, clock, merchant_id, MEMORY_SCOPE, source=f"deletion_request:{requested_by}")
        if ledger is not None:
            ledger.append(kind="STATE", actor="SYSTEM", payload={"event": "MERCHANT_MEMORY_DELETED", "merchant_id": merchant_id,
                                                                "counts": counts, "kept": sorted(LEGALLY_REQUIRED)})
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    return counts
