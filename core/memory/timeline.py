"""Per-merchant timeline (Bible section 33): all cases, events and outcomes in order, plus a token-lean form for model context.

Every query goes through ``_q`` which always binds the merchant id; event rows are additionally required to belong to
that merchant (or to none), and any other merchant's id found inside a payload is masked, so a shared query path cannot
leak B into A's context.
"""
from __future__ import annotations

import json
import re
import sqlite3

from core.engine.clock import SimClock
from core.memory.profile import MEMORY_SCOPE, ProfileStore, memory_allowed

_MAX_VAL = 40


class Timeline:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock):
        self.conn, self.clock = conn, clock
        self.profile = ProfileStore(conn, clock)

    def _q(self, sql: str, merchant_id: str, *extra):
        return self.conn.execute(sql, (merchant_id, *extra)).fetchall()

    def _other_ids_re(self, merchant_id: str):
        ids = [r[0] for r in self.conn.execute("SELECT merchant_id FROM merchants WHERE merchant_id <> ?", (merchant_id,))]
        return re.compile("|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True))) if ids else None

    def build(self, merchant_id: str) -> dict:
        """{'merchant_id', 'cases': [{case, events, summary}], 'facts': {...}} ordered by time."""
        if not memory_allowed(self.conn, merchant_id):
            return {"merchant_id": merchant_id, "cases": [], "facts": {}, "memory": "WITHDRAWN"}
        mask = self._other_ids_re(merchant_id)

        def clean(s):
            return mask.sub("[other]", s) if (mask and isinstance(s, str)) else s

        cases = []
        for c in self._q("SELECT * FROM cases WHERE merchant_id = ? ORDER BY opened_at, case_id", merchant_id):
            evs = self._q(
                "SELECT e.event_id, e.type, e.payload_json, e.ts_sim FROM case_events ce JOIN events e ON e.event_id = ce.event_id "
                "JOIN cases c ON c.case_id = ce.case_id WHERE c.merchant_id = ? AND ce.case_id = ? "
                "AND (e.merchant_id IS NULL OR e.merchant_id = c.merchant_id) ORDER BY e.ts_sim, e.seq", merchant_id, c["case_id"])
            ep = self._q("SELECT summary_json FROM episodes WHERE merchant_id = ? AND case_id = ?", merchant_id, c["case_id"])
            summary = [s["text"] for s in json.loads(ep[0]["summary_json"]).get("sentences", [])] if ep else []
            cases.append({
                "case_id": c["case_id"], "playbook": c["playbook_id"], "state": c["state"], "opened_at": c["opened_at"],
                "closed_at": c["closed_at"],
                "events": [{"event_id": e["event_id"], "type": e["type"], "ts": e["ts_sim"],
                            "payload": json.loads(clean(e["payload_json"]))} for e in evs],
                "summary": [clean(s) for s in summary],
            })
        facts = {k: {"value": clean(f.value), "source": f.source, "inferred": f.inferred}
                 for k, f in self.profile.current(merchant_id).items()}
        return {"merchant_id": merchant_id, "cases": cases, "facts": facts}

    def compact(self, merchant_id: str, max_events: int = 6) -> str:
        """Token-lean text: one header line, cross-case facts, then one line per case with its latest events."""
        t = self.build(merchant_id)
        if t.get("memory") == "WITHDRAWN":
            return f"merchant {merchant_id}: memory use withdrawn by the merchant"
        lines = [f"merchant {merchant_id}"]
        flags = []
        if self.profile.is_do_not_contact(merchant_id):
            flags.append("DO_NOT_CONTACT")
        flags += [f"DECLINED({k},until={v[:10]})" for k, v in sorted(self.profile.declined_until(merchant_id).items())]
        if flags:
            lines.append("flags: " + " ".join(flags))
        facts = [f"{k}={v['value']}" + ("~inferred" if v["inferred"] else "") for k, v in sorted(t["facts"].items())
                 if not (k == "do_not_contact" or k.startswith("declined:"))]
        if facts:
            lines.append("facts: " + "; ".join(facts))
        for c in t["cases"]:
            evs = c["events"][-max_events:]
            ev = ",".join(f"{e['type']}@{e['ts'][5:16]}" for e in evs)
            lines.append(f"case {c['case_id']} {c['playbook']} {c['state']} {c['opened_at'][:10]}"
                         + (f" events[{ev}]" if ev else "") + (f" | {' '.join(c['summary'])}" if c["summary"] else ""))
        return "\n".join(lines)
