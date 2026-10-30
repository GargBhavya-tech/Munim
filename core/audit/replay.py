"""Replay a case from the ledger alone: no model calls, no network, no wall clock.

Replay folds a case's ledger entries (in order) into a state summary. It re-verifies the whole
hash chain first, so a replay of tampered history says so instead of quietly succeeding.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from core.audit.ledger import Ledger, LedgerEntry, VerifyResult, canonical_json, verify_chain
from core.engine.clock import SimClock


class ReplayError(Exception):
    pass


@dataclass
class ReplayResult:
    case_id: str
    chain: VerifyResult
    state: dict
    steps: list[dict] = field(default_factory=list)
    entry_hashes: list[str] = field(default_factory=list)

    @property
    def state_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.state).encode("utf-8")).hexdigest()


def initial_state() -> dict:
    return {
        "status": "NEW", "step": None, "events": [], "decisions": 0, "policy_decisions": [], "actions": [],
        "compensations": [], "messages": [], "escalations": [], "human_actions": [], "verifications": 0,
    }


def apply_entry(state: dict, e: LedgerEntry) -> str:
    """Fold one entry into the state; return a one-line description for the step trace."""
    p = e.payload
    k = e.kind
    if k == "STATE":
        state["status"], state["step"] = p.get("state", state["status"]), p.get("step", state["step"])
        return f"state -> {state['status']}" + (f" (step {state['step']})" if state["step"] else "")
    if k == "EVENT":
        state["events"].append(p.get("type") or p.get("event_id"))
        return f"event {p.get('type')}"
    if k == "DECISION":
        state["decisions"] += 1
        return f"decision {p.get('next_action') or 'reply only'}"
    if k == "VERIFY":
        state["verifications"] += 1
        return f"verify {p.get('result', '')}".strip()
    if k == "POLICY":
        state["policy_decisions"].append({"action": p.get("action"), "allow": p.get("allow"), "rule_ids": p.get("rule_ids", [])})
        return f"policy {'allow' if p.get('allow') else 'refuse'} {p.get('action')}"
    if k == "ACTION_INTENT":
        return f"intent {p.get('action') or p.get('kind')}"
    if k == "ACTION_RESULT":
        state["actions"].append({"action": p.get("action"), "key": p.get("key"), "status": p.get("status")})
        return f"result {p.get('action')} -> {p.get('status')}"
    if k == "COMPENSATION":
        state["compensations"].append({"action": p.get("action"), "status": p.get("status")})
        return f"compensation {p.get('action')}"
    if k == "MESSAGE_SENT":
        state["messages"].append({"language": p.get("language"), "text": p.get("text")})
        return "message sent"
    if k == "ESCALATION":
        state["escalations"].append(p.get("reasons", []))
        state["status"] = "ESCALATED"
        return f"escalated {p.get('reasons', [])}"
    if k == "HUMAN_ACTION":
        state["human_actions"].append({"role": e.role, "action": p.get("action")})
        return f"human {e.role}: {p.get('action')}"
    return k.lower()


def replay_case(conn: sqlite3.Connection, case_id: str) -> ReplayResult:
    chain = verify_chain(conn)
    entries = Ledger(conn, SimClock()).entries(case_id)
    if not entries:
        raise ReplayError(f"no ledger entries for case {case_id!r}")
    state = initial_state()
    steps = []
    for e in entries:
        steps.append({"seq": e.seq, "ts_sim": e.ts_sim, "kind": e.kind, "actor": e.actor, "summary": apply_entry(state, e)})
    return ReplayResult(case_id, chain, state, steps, [e.hash for e in entries])
