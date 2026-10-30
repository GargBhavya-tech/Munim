"""Rate and cost caps for Judge Mode (#129). Checked BEFORE the engine is called; a refusal writes nothing to the ledger and touches no case.

Usage is read from the ledger (attempt entries, and the token counts the LLM client records). Cost uses a SYNTHETIC rate (not a real price).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from core.engine.clock import parse_iso
from core.judge.session import ACTOR, OP_ATTEMPT, JudgeSession

SIM_COST_PAISE_PER_1K_TOKENS = 10      # SIMULATED rate, integer paise


@dataclass(frozen=True)
class JudgeCaps:
    max_messages: int = 40            # per session
    max_tokens: int = 60_000          # LLM tokens (in + out) per session
    max_cost_paise: int = 600         # per session, at the synthetic rate
    burst_messages: int = 8           # at most this many attempts ...
    burst_window_s: int = 60          # ... in this many sim-clock seconds

    def as_json(self) -> dict:
        return {"max_messages": self.max_messages, "max_tokens": self.max_tokens, "max_cost_paise": self.max_cost_paise,
                "burst_messages": self.burst_messages, "burst_window_s": self.burst_window_s}


@dataclass(frozen=True)
class CapRefusal:
    cap: str
    limit: int
    used: int

    def as_json(self) -> dict:
        return {"error": "JUDGE_CAP", "cap": self.cap, "limit": self.limit, "used": self.used,
                "reason": f"Judge session cap reached: {self.cap} ({self.used} of {self.limit}). Nothing was sent to the engine."}


def cost_paise(tokens: int) -> int:
    return -(-tokens * SIM_COST_PAISE_PER_1K_TOKENS // 1000)


def usage(conn: sqlite3.Connection, s: JudgeSession, now_iso: str, caps: JudgeCaps) -> dict:
    rows = conn.execute("SELECT ts_sim FROM ledger WHERE seq > ? AND kind = 'HUMAN_ACTION' AND actor = ? AND json_extract(payload_json, '$.op') = ? "
                        "AND json_extract(payload_json, '$.session_id') = ?", (s.start_seq, ACTOR, OP_ATTEMPT, s.session_id)).fetchall()
    now = parse_iso(now_iso)
    recent = sum(1 for r in rows if (now - parse_iso(r[0])).total_seconds() < caps.burst_window_s)
    tokens = 0
    for (pj,) in conn.execute("SELECT l.payload_json FROM ledger l LEFT JOIN cases c ON c.case_id = l.case_id "
                              "WHERE l.seq > ? AND l.kind = 'CONTEXT_HASH' AND (c.merchant_id = ? OR l.case_id IS NULL)", (s.start_seq, s.merchant_id)):
        p = json.loads(pj)
        tokens += int(p.get("tokens_in") or 0) + int(p.get("tokens_out") or 0)
    return {"messages": len(rows), "recent_messages": recent, "tokens": tokens, "cost_paise": cost_paise(tokens)}


def check(conn: sqlite3.Connection, s: JudgeSession, caps: JudgeCaps, now_iso: str) -> CapRefusal | None:
    u = usage(conn, s, now_iso, caps)
    if u["messages"] >= caps.max_messages:
        return CapRefusal("max_messages", caps.max_messages, u["messages"])
    if u["recent_messages"] >= caps.burst_messages:
        return CapRefusal("burst_messages", caps.burst_messages, u["recent_messages"])
    if u["tokens"] >= caps.max_tokens:
        return CapRefusal("max_tokens", caps.max_tokens, u["tokens"])
    if u["cost_paise"] >= caps.max_cost_paise:
        return CapRefusal("max_cost_paise", caps.max_cost_paise, u["cost_paise"])
    return None
