"""Area incidents (I1, #88; Bible section 22): many merchants failing together is ONE incident, not many repairs.

  detect    RecurrenceJob's candidates: >= 10 devices in ONE PINCODE losing heartbeat within 10 minutes, or >= 10 merchants' settlements
            failing on ONE BANK within 30 minutes (grouping by city is the named mistake). Overlapping candidates of the same area and kind
            join the one OPEN incident.
  graph     a NetworkX impact graph area -> device -> merchant -> case, built only from database rows. The model never adds an edge.
  hold      every affected merchant goes on an incident hold: the policy rule INCIDENT-HOLD-001 blocks per-merchant replacements and visits
            for them, and their open fault cases are PARKED with a note (a replacement cannot fix an area outage).
  notify    exactly ONE message per affected merchant per incident (incident_notices is the guard), through the executor, so consent,
            quiet hours and the contact budget still apply. Text is a fixed template; the pincode / bank come from system rows.
  resolve   releases the holds and un-parks the cases, which re-diagnose against live state.
Message templates: NATIVE-SPEAKER REVIEW EXPECTED for hi-IN, hinglish and kn-IN.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import networkx as nx

from core.actions.types import ActionIntent, Principal
from core.engine.clock import SimClock
from core.memory.store import next_id, write_txn
from jobs.recurrence import RecurrenceJob

SYSTEM_INCIDENTS = Principal("SYSTEM", "incident_manager")
AFFECTED_PLAYBOOKS = {"HEARTBEAT_PINCODE": ("C2_SOUNDBOX_FAULT",), "SETTLEMENT_BANK": ("C1_SETTLEMENT_DELAY",)}

SCHEMA = """   -- the incidents table itself is in core/memory/schema.sql (detail_json holds {"candidates": [...]})
CREATE TABLE IF NOT EXISTS incident_members (
  incident_id TEXT NOT NULL, merchant_id TEXT NOT NULL, device_id TEXT, PRIMARY KEY (incident_id, merchant_id)
);
CREATE TABLE IF NOT EXISTS incident_holds (
  merchant_id TEXT NOT NULL, incident_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (merchant_id, incident_id)
);
CREATE TABLE IF NOT EXISTS incident_notices (
  incident_id TEXT NOT NULL, merchant_id TEXT NOT NULL, status TEXT NOT NULL, action_status TEXT, PRIMARY KEY (incident_id, merchant_id)
);
"""

TEXT = {
    "HEARTBEAT_PINCODE": {
        "en-IN": "Our team is fixing a problem affecting Soundbox devices in your area ({area}). You do not need a replacement. We will tell you when it is fixed.",
        "hinglish": "Aapke area ({area}) mein Soundbox devices mein ek problem hai, hamari team use theek kar rahi hai. Aapko naya device nahi chahiye. Theek hote hi hum aapko batayenge.",
        "hi-IN": "आपके इलाके ({area}) में साउंडबॉक्स डिवाइसों में एक समस्या है, हमारी टीम उसे ठीक कर रही है। आपको नया डिवाइस नहीं चाहिए। ठीक होते ही हम आपको बताएँगे।",
        "kn-IN": "ನಿಮ್ಮ ಪ್ರದೇಶದಲ್ಲಿ ({area}) ಸೌಂಡ್‌ಬಾಕ್ಸ್ ಸಾಧನಗಳಲ್ಲಿ ಒಂದು ಸಮಸ್ಯೆ ಇದೆ, ನಮ್ಮ ತಂಡ ಸರಿಪಡಿಸುತ್ತಿದೆ. ನಿಮಗೆ ಹೊಸ ಸಾಧನ ಬೇಡ. ಸರಿಯಾದ ತಕ್ಷಣ ತಿಳಿಸುತ್ತೇವೆ.",
    },
    "SETTLEMENT_BANK": {
        "en-IN": "Settlements to {area} bank accounts are delayed by a problem on the bank's side. Our team is on it; you do not need to change anything.",
        "hinglish": "{area} bank accounts mein settlement bank ki taraf ki problem ki wajah se late ho rahe hain. Hamari team is par kaam kar rahi hai; aapko kuch badalne ki zaroorat nahi.",
        "hi-IN": "{area} बैंक खातों में सेटलमेंट बैंक की तरफ़ की समस्या के कारण देर से आ रहे हैं। हमारी टीम इस पर काम कर रही है; आपको कुछ बदलने की ज़रूरत नहीं।",
        "kn-IN": "{area} ಬ್ಯಾಂಕ್ ಖಾತೆಗಳಿಗೆ ಸೆಟಲ್‌ಮೆಂಟ್ ಬ್ಯಾಂಕ್ ಕಡೆಯ ಸಮಸ್ಯೆಯಿಂದ ತಡವಾಗುತ್ತಿದೆ. ನಮ್ಮ ತಂಡ ಕೆಲಸ ಮಾಡುತ್ತಿದೆ; ನೀವು ಏನನ್ನೂ ಬದಲಿಸಬೇಕಾಗಿಲ್ಲ.",
    },
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def on_hold(conn: sqlite3.Connection, merchant_id: str) -> str | None:
    """The OPEN incident holding this merchant's per-merchant fixes, if any (read by policy rule INCIDENT-HOLD-001)."""
    try:
        r = conn.execute("SELECT h.incident_id FROM incident_holds h JOIN incidents i ON i.incident_id = h.incident_id "
                         "WHERE h.merchant_id = ? AND h.active = 1 AND i.status = 'OPEN' LIMIT 1", (merchant_id,)).fetchone()
    except sqlite3.OperationalError:          # the incident tables do not exist yet: nothing is on hold
        return None
    return r[0] if r else None


@dataclass(frozen=True)
class RunResult:
    incidents: list[str]
    newly_held: int
    notices_sent: int


class IncidentManager:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, executor, case_manager, repo):
        self.conn, self.clock, self.ledger, self.executor = conn, clock, ledger, executor
        self.case_manager, self.repo = case_manager, repo
        self.recurrence = RecurrenceJob(conn, clock)
        ensure_schema(conn)

    # ------------------------------------------------------------------------------------------------------- run
    def run(self) -> RunResult:
        cands = self._detect()
        touched, held = [], 0
        for cid in dict.fromkeys(cands):
            c = self.conn.execute("SELECT * FROM incident_candidates WHERE candidate_id = ?", (cid,)).fetchone()
            iid = self._incident_for(c)
            touched.append(iid)
            held += self._add_members(iid, c)
        sent = sum(self._notify(iid) for iid in dict.fromkeys(touched))
        return RunResult(list(dict.fromkeys(touched)), held, sent)

    def _detect(self) -> list[str]:
        """RecurrenceJob keys a candidate by its window start with INSERT OR IGNORE, so a device that goes dark AFTER the first run, inside
        the same window, would never reach the stored row. The candidate rows are derived data (rebuilt from the events table on every run),
        so the ones inside the lookback are dropped and re-derived: late joiners are seen, and _add_members stays idempotent."""
        from datetime import timedelta
        from core.engine.clock import iso
        since = iso(self.clock.now() - timedelta(hours=24))        # RecurrenceJob.detect_incident_candidates' default lookback
        with write_txn(self.conn):
            self.conn.execute("DELETE FROM incident_candidates WHERE window_start >= ?", (since,))
        return self.recurrence.detect_incident_candidates()

    def _incident_for(self, c: sqlite3.Row) -> str:
        r = self.conn.execute("SELECT incident_id, detail_json FROM incidents WHERE kind = ? AND area = ? AND status = 'OPEN'",
                              (c["kind"], c["area"])).fetchone()
        with write_txn(self.conn):
            if r is not None:
                detail = json.loads(r["detail_json"] or "{}")
                ids = detail.get("candidates", [])
                if c["candidate_id"] not in ids:
                    detail["candidates"] = ids + [c["candidate_id"]]
                    self.conn.execute("UPDATE incidents SET detail_json = ? WHERE incident_id = ?", (json.dumps(detail), r["incident_id"]))
                return r["incident_id"]
            iid = next_id(self.conn, "INC")
            self.conn.execute("INSERT INTO incidents(incident_id, kind, area, status, opened_at, detail_json) VALUES (?,?,?, 'OPEN', ?,?)",
                              (iid, c["kind"], c["area"], self.clock.now_iso(), json.dumps({"candidates": [c["candidate_id"]]})))
            self.ledger.append(kind="ESCALATION", actor="SYSTEM", payload={"incident_id": iid, "kind": c["kind"], "area": c["area"],
                                                                           "reasons": [f"AREA_INCIDENT: {c['kind']} {c['area']}"], "owner_role": "ops_admin"})
            return iid

    def _merchants_of(self, c: sqlite3.Row) -> list[tuple[str, str | None]]:
        members = json.loads(c["members_json"])
        if c["kind"] == "HEARTBEAT_PINCODE":                          # members are device ids (or merchant ids when the event had none)
            out = []
            for m in members:
                r = self.conn.execute("SELECT merchant_id, device_id FROM devices WHERE device_id = ? OR merchant_id = ?", (m, m)).fetchone()
                if r:
                    out.append((r["merchant_id"], r["device_id"]))
            return out
        return [(m, None) for m in members]

    def _add_members(self, iid: str, c: sqlite3.Row) -> int:
        inc = self.conn.execute("SELECT kind FROM incidents WHERE incident_id = ?", (iid,)).fetchone()
        n = 0
        for mid, dev in self._merchants_of(c):
            with write_txn(self.conn):
                cur = self.conn.execute("INSERT OR IGNORE INTO incident_members VALUES (?,?,?)", (iid, mid, dev))
                if cur.rowcount == 0:
                    continue
                self.conn.execute("INSERT OR IGNORE INTO incident_holds(merchant_id, incident_id) VALUES (?,?)", (mid, iid))
            n += 1
            for case in self.repo.open_cases(mid):
                if case.playbook_id in AFFECTED_PLAYBOOKS.get(inc["kind"], ()) and case.state not in ("PARKED", "ESCALATED"):
                    self.case_manager.park(case, f"Part of area incident {iid}: no per-merchant fix while it is open.")
        return n

    def _notify(self, iid: str) -> int:
        inc = self.conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (iid,)).fetchone()
        sent = 0
        for n, m in enumerate(self.conn.execute("SELECT im.merchant_id, mc.language FROM incident_members im JOIN merchants mc ON mc.merchant_id = im.merchant_id "
                                                "WHERE im.incident_id = ? ORDER BY im.merchant_id", (iid,)).fetchall()):
            with write_txn(self.conn):
                self.conn.execute("INSERT OR IGNORE INTO incident_notices(incident_id, merchant_id, status) VALUES (?,?, 'PENDING')", (iid, m["merchant_id"]))
                prior = self.conn.execute("SELECT status FROM incident_notices WHERE incident_id = ? AND merchant_id = ?", (iid, m["merchant_id"])).fetchone()[0]
            if prior == "SENT":
                continue                                                # one message per merchant per incident, ever
            # PENDING or NOT_SENT (quiet hours, a stated window, no consent): tried again on the next run, never sent twice
            lang = m["language"] if m["language"] in TEXT[inc["kind"]] else "en-IN"
            body = TEXT[inc["kind"]][lang].format(area=inc["area"])
            out = self.executor.submit(ActionIntent("send_message", {"merchant_id": m["merchant_id"], "channel": "whatsapp", "body": body, "language": lang},
                                                    case_id=iid, step_seq=n + 1, principal=SYSTEM_INCIDENTS, channel="whatsapp", proactive=True))
            with write_txn(self.conn):
                self.conn.execute("UPDATE incident_notices SET status = ?, action_status = ? WHERE incident_id = ? AND merchant_id = ?",
                                  ("SENT" if out.status == "OK" else "NOT_SENT", out.status, iid, m["merchant_id"]))
            sent += out.status == "OK"
        return sent

    def hold_note(self, case) -> str | None:
        """The engine's ``hold_fn``: an affected fault case of a held merchant parks before it acts (no checklist, no replacement);
        the incident notice is that merchant's one message."""
        iid = on_hold(self.conn, case.merchant_id) if case.merchant_id else None
        if iid is None:
            return None
        kind = self.conn.execute("SELECT kind FROM incidents WHERE incident_id = ?", (iid,)).fetchone()["kind"]
        return f"Part of area incident {iid}: no per-merchant fix while it is open." if case.playbook_id in AFFECTED_PLAYBOOKS.get(kind, ()) else None

    # ------------------------------------------------------------------------------------------------------- graph
    def impact_graph(self, iid: str) -> nx.DiGraph:
        """area -> device -> merchant -> case, every node and edge from a database row (no model involved)."""
        inc = self.conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (iid,)).fetchone()
        g = nx.DiGraph(incident=iid)
        area = f"area:{inc['area']}"
        g.add_node(area, kind="pincode" if inc["kind"] == "HEARTBEAT_PINCODE" else "bank")
        for m in self.conn.execute("SELECT merchant_id, device_id FROM incident_members WHERE incident_id = ?", (iid,)):
            mnode = f"merchant:{m['merchant_id']}"
            g.add_node(mnode, kind="merchant")
            if m["device_id"]:
                dnode = f"device:{m['device_id']}"
                g.add_node(dnode, kind="device")
                g.add_edge(area, dnode)
                g.add_edge(dnode, mnode)
            else:
                g.add_edge(area, mnode)
            for c in self.conn.execute("SELECT case_id, playbook_id, state FROM cases WHERE merchant_id = ? AND state NOT IN ('RESOLVED', 'DECLINED')",
                                       (m["merchant_id"],)):
                g.add_node(f"case:{c['case_id']}", kind="case", playbook=c["playbook_id"], state=c["state"])
                g.add_edge(mnode, f"case:{c['case_id']}")
        return g

    # ------------------------------------------------------------------------------------------------------- resolve
    def resolve(self, iid: str) -> list[str]:
        with write_txn(self.conn):
            self.conn.execute("UPDATE incidents SET status = 'RESOLVED', closed_at = ? WHERE incident_id = ?", (self.clock.now_iso(), iid))
            self.conn.execute("UPDATE incident_holds SET active = 0 WHERE incident_id = ?", (iid,))
        released = []
        for m in self.conn.execute("SELECT merchant_id FROM incident_members WHERE incident_id = ?", (iid,)).fetchall():
            if on_hold(self.conn, m["merchant_id"]):
                continue                                                # still held by another open incident
            for case in self.repo.open_cases(m["merchant_id"]):
                if case.state == "PARKED" and (case.resume_note or "").startswith(f"Part of area incident {iid}"):
                    case = self.repo.transition(case, "ACTIVE", reason=f"incident {iid} resolved")
                    self.repo.transition(case, "WAITING", reason="waiting")
                    released.append(case.case_id)
        self.ledger.append(kind="STATE", actor="SYSTEM", payload={"incident_id": iid, "status": "RESOLVED", "released_cases": released})
        return released


__all__ = ["IncidentManager", "on_hold", "ensure_schema"]
