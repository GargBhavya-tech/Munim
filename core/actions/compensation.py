"""Compensation registry and Undo (#27).

On success an agent-callable R1/R2 registers {compensation action, built args, compensable_until} in the
``compensations`` table. Undo runs the compensation through the SAME executor pipeline (policy, idempotency,
breaker, ledger). Undo is idempotent: the second call returns the stored result without touching the mock.
The window is enforced here, in code, on the sim clock.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass
from core.actions.types import ActionIntent, ActionOutcome, Principal, Violation
from core.engine.clock import SimClock, iso, parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from core.actions.executor import Executor

OPEN_WINDOW = "9999-12-31T23:59:59+05:30"   # R1 / "window_hours None": compensable while the object is still open
SYSTEM = Principal("SYSTEM", "munim-undo")


class CompensationRegistry:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, registry: ActionRegistry):
        self.conn, self.clock, self.registry = conn, clock, registry

    def eligible(self, spec: ActionSpec) -> bool:
        return spec.agent_callable and spec.cls in (RiskClass.R1, RiskClass.R2) and bool(spec.compensation) and spec.comp_args is not None

    def register(self, action_id: str, spec: ActionSpec, args: dict, result_body: dict) -> str | None:
        """Caller holds the transaction. Returns compensable_until (None = open while the object is open)."""
        if not self.eligible(spec):
            return None
        comp_args = spec.comp_args(args, result_body)
        until = OPEN_WINDOW if spec.window_hours is None else iso(self.clock.now() + timedelta(hours=spec.window_hours))
        self.conn.execute(
            "INSERT OR IGNORE INTO compensations(action_id, compensation_json, compensable_until, status) VALUES (?,?,?, 'OPEN')",
            (action_id, json.dumps({"action": spec.compensation, "args": comp_args, "of": spec.name}, sort_keys=True), until))
        return None if until == OPEN_WINDOW else until

    def get(self, action_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM compensations WHERE action_id = ?", (action_id,)).fetchone()
        return dict(r) if r else None

    def in_window(self, row: dict) -> bool:
        return self.clock.now() <= parse_iso(row["compensable_until"])

    def mark_done(self, action_id: str, result: dict) -> None:
        self.conn.execute("UPDATE compensations SET status = 'DONE', result_json = ? WHERE action_id = ?",
                          (json.dumps(result, sort_keys=True), action_id))


def undo(ex: "Executor", action_id: str, principal: Principal) -> ActionOutcome:
    comps = ex.compensations
    row = comps.get(action_id)
    act = ex.conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
    if row is None or act is None:
        return ActionOutcome("ERROR", "", action_id=action_id, error=f"NOT_COMPENSABLE: no compensation is registered for {action_id}")
    case_id = act["case_id"]
    comp = json.loads(row["compensation_json"])

    def ledger(status: str, **extra) -> None:
        ex.ledger.append(kind="COMPENSATION", actor=principal.ledger_actor, role=principal.role, case_id=case_id,
                         payload={"action": comp["action"], "status": status, "of_action_id": action_id, "of": comp["of"], **extra})

    if row["status"] == "DONE":                      # undo twice: a no-op returning the stored result
        stored = json.loads(row["result_json"]) if row["result_json"] else {}
        ledger("REPLAYED")
        return ActionOutcome("REPLAYED", stored.get("key", ""), action_id=stored.get("action_id"), result=stored.get("result"))

    if not comps.in_window(row):
        reason = f"WINDOW_CLOSED: the undo window for {action_id} ({comp['of']}) closed at {row['compensable_until']}"
        ledger("REFUSED", reason=reason)
        return ActionOutcome("REFUSED", "", action_id=action_id, error=reason,
                             violations=[Violation("COMP-WINDOW", "Undo window closed", "BLOCK", reason, action=comp["action"], case_id=case_id)])

    intent = ActionIntent(action=comp["action"], args=comp["args"], case_id=case_id, step_seq=act["step_seq"] or 0, principal=principal)
    out = ex._submit(intent, bypass_kill=True)       # same pipeline; undo is risk-reducing so the kill switch does not block it
    if out.status in ("OK", "REPLAYED"):
        with ex._txn():
            comps.mark_done(action_id, {"key": out.idempotency_key, "action_id": out.action_id, "result": out.result})
        ledger("OK", key=out.idempotency_key, compensation_action_id=out.action_id)
        return ActionOutcome("OK", out.idempotency_key, action_id=out.action_id, result=out.result)
    ledger(out.status, error=out.error)
    return out


def compensate_pending_r1(ex: "Executor", case_id: str) -> tuple[list[ActionOutcome], list[dict]]:
    """DECLINED / kill: undo every still-open R1 of the case automatically; hand the open R2s to a human."""
    rows = ex.conn.execute(
        "SELECT a.action_id, a.name, a.cls, c.compensation_json, c.compensable_until FROM compensations c"
        " JOIN actions a ON a.action_id = c.action_id WHERE a.case_id = ? AND c.status = 'OPEN' ORDER BY a.rowid", (case_id,)).fetchall()
    done, for_human = [], []
    for r in rows:
        if r["cls"] == "R1":
            done.append(undo(ex, r["action_id"], SYSTEM))
        else:
            for_human.append({"action_id": r["action_id"], "action": r["name"], "compensation": json.loads(r["compensation_json"])["action"],
                              "compensable_until": r["compensable_until"]})
    return done, for_human
