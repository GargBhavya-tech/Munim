"""CRM gateway with field-level permissions (Bible section 33, "CRM access").

Agent read+write: activity log, conversation summaries, stage, next action/due, intent tags/objections/language preference,
health and churn scores. Agent read-only: contract/pricing, device inventory/status, aggregated payment history, open
tickets, assigned rep. Agent never touches: KYC documents, bank details, credit limits/loan decisions, settlement accounts.
Nothing can be deleted through this gateway. Every agent write is ADDED (history stays), tagged ``written_by='agent'``,
and a field whose latest value was set by a human is refused for the agent (human edits win).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.engine.clock import SimClock

AGENT_RW = frozenset({"activity_log", "conversation_summary", "stage", "next_action", "next_action_due", "intent_tags",
                      "objections", "language_preference", "health_score", "churn_score"})
AGENT_RO = frozenset({"contract_plan", "pricing_plan", "device_inventory", "device_status", "payment_history_agg",
                      "open_tickets", "assigned_rep"})
AGENT_NEVER = frozenset({"kyc_documents", "bank_account", "credit_limit", "loan_decision", "settlement_account"})
# Fields that are a growing list of entries: an agent entry never replaces a human's entry, it is one more entry.
APPEND_ONLY_FIELDS = frozenset({"activity_log", "conversation_summary", "intent_tags", "objections"})

_DDL = """
CREATE TABLE IF NOT EXISTS crm_fields (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT NOT NULL, field TEXT NOT NULL, value TEXT,
  written_by TEXT NOT NULL, actor TEXT, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crm_fields ON crm_fields(merchant_id, field, version_id);
CREATE TABLE IF NOT EXISTS crm_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT, field TEXT, op TEXT NOT NULL, outcome TEXT NOT NULL,
  reason TEXT, ts TEXT NOT NULL
);
"""


class CRMPermissionError(PermissionError):
    pass


@dataclass(frozen=True)
class CRMEntry:
    version_id: int
    merchant_id: str
    field: str
    value: str | None
    written_by: str  # 'agent' | 'human' | 'system'
    actor: str | None
    ts: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _DDL.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _e(r) -> CRMEntry:
    return CRMEntry(r["version_id"], r["merchant_id"], r["field"], r["value"], r["written_by"], r["actor"], r["ts"])


class CRMGateway:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock
        ensure_schema(conn)

    def _log(self, merchant_id, field, op, outcome, reason=None):
        self.conn.execute("INSERT INTO crm_log(merchant_id,field,op,outcome,reason,ts) VALUES (?,?,?,?,?,?)",
                          (merchant_id, field, op, outcome, reason, self.clock.now_iso()))

    def _refuse(self, merchant_id, field, op, reason):
        self._log(merchant_id, field, op, "REFUSED", reason)
        raise CRMPermissionError(reason)

    def can_read(self, field: str) -> bool:
        return field in AGENT_RW or field in AGENT_RO

    def can_write(self, field: str) -> bool:
        return field in AGENT_RW

    # -- agent API ---------------------------------------------------------------------------------
    def agent_read(self, merchant_id: str, field: str) -> list[CRMEntry] | CRMEntry | None:
        """Append fields return every entry (oldest first); single-value fields return the current entry (or None)."""
        if field in AGENT_NEVER:
            self._refuse(merchant_id, field, "READ", f"agent may never touch {field}")
        if not self.can_read(field):
            self._refuse(merchant_id, field, "READ", f"unknown CRM field {field}")
        hist = self.history(merchant_id, field)
        if field in APPEND_ONLY_FIELDS:
            return hist
        return hist[-1] if hist else None

    def agent_write(self, merchant_id: str, field: str, value, actor: str = "munim") -> CRMEntry:
        if field in AGENT_NEVER:
            self._refuse(merchant_id, field, "WRITE", f"agent may never touch {field}")
        if field in AGENT_RO:
            self._refuse(merchant_id, field, "WRITE", f"{field} is read-only for the agent")
        if field not in AGENT_RW:
            self._refuse(merchant_id, field, "WRITE", f"unknown CRM field {field}")
        if field not in APPEND_ONLY_FIELDS:
            hist = self.history(merchant_id, field)
            if hist and hist[-1].written_by == "human":
                self._refuse(merchant_id, field, "WRITE", f"{field} was edited by a human; the agent cannot change it")
        return self._add(merchant_id, field, value, "agent", actor)

    def agent_delete(self, merchant_id: str, field: str):
        self._refuse(merchant_id, field, "DELETE", "the agent can never delete anything")

    # -- human / system API (not agent-permission checked, but never KYC/bank/credit data here) -----
    def human_edit(self, merchant_id: str, field: str, value, editor: str) -> CRMEntry:
        if field in AGENT_NEVER:
            raise CRMPermissionError(f"{field} is not stored in the agent CRM store")
        if not self.can_read(field):
            raise CRMPermissionError(f"unknown CRM field {field}")
        return self._add(merchant_id, field, value, "human", editor)

    def system_load(self, merchant_id: str, field: str, value, source: str = "system") -> CRMEntry:
        """Platform data mirrored in for the agent to READ (plan, device status...)."""
        if field in AGENT_NEVER or not self.can_read(field):
            raise CRMPermissionError(f"{field} cannot be loaded here")
        return self._add(merchant_id, field, value, "system", source)

    def history(self, merchant_id: str, field: str) -> list[CRMEntry]:
        rs = self.conn.execute("SELECT * FROM crm_fields WHERE merchant_id=? AND field=? ORDER BY version_id", (merchant_id, field))
        return [_e(r) for r in rs]

    def _add(self, merchant_id, field, value, written_by, actor) -> CRMEntry:
        c = self.conn.execute("INSERT INTO crm_fields(merchant_id,field,value,written_by,actor,ts) VALUES (?,?,?,?,?,?)",
                              (merchant_id, field, None if value is None else str(value), written_by, actor, self.clock.now_iso()))
        self._log(merchant_id, field, "WRITE", "OK", written_by)
        return _e(self.conn.execute("SELECT * FROM crm_fields WHERE version_id=?", (c.lastrowid,)).fetchone())
