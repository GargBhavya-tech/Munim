"""Questions to the ledger (#123): an auditor asks in words, the answer comes from a FIXED set of read-only queries.

Nothing a person types is ever run as SQL, and no model writes SQL. A question is matched to a template by keywords; the only things taken from the
text are a day (today, yesterday or an ISO date), a case id, a merchant id or a rule id, each by a strict pattern and each BOUND as a parameter.
Text that looks like SQL is refused before anything is matched, and an unmatched question runs no query at all and lists what can be asked.

* Role: only an auditor may ask (the ledger holds every case). Others get ``NotAllowed``.
* Read-only: every query runs on its own connection set to ``query_only``.
* Rows link back to the ledger (seq and a path); a row carries a short summary built from fixed fields, never the free text of a message.
Keyword lists are English plus Hindi, Kannada and Hinglish terms; the non-English terms need native-speaker review.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

AUDIT_ROLE = "auditor"
MAX_ROWS = 200
_SQLISH = re.compile(r"(;|--|/\*|\*/|\b(select|insert|update|delete|drop|alter|attach|pragma|union|create|replace)\b|\bfrom\s+\w+|\bwhere\b)", re.I)
_DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_CASE = re.compile(r"\b(CS-\d{4,6})\b", re.I)
_MERCHANT = re.compile(r"\b(M-\d{4})\b", re.I)
_RULE = re.compile(r"\b([A-Z]+(?:-[A-Z]+)*-\d{3})\b")

LOAN = ("loan", "lone", "लोन", "कर्ज", "ऋण", "ಸಾಲ", "ಲೋನ್", "karz", "udhaar")
REFUSED = ("refus", "denied", "blocked", "reject", "declin", "inkaar", "mana kar", "इनकार", "मना", "रोक", "ನಿರಾಕ", "ತಿರಸ್ಕ")
ESCALATE = ("escalat", "handoff", "hand-off", "handed off", "human", "इंसान", "वृद्धि", "ಹಸ್ತಾಂತರ")
CLAIM = ("claim", "unsupported", "unproven", "red chip", "blocked claim", "दावा", "ಹೇಳಿಕೆ")
ACTION = ("action", "executed", "did munim do", "what was done", "कार्रवाई", "ಕ್ರಮ")
RULE_WORDS = ("rule", "नियम", "ನಿಯಮ")
FORBIDDEN = ("forbidden", "unknown action", "not allowed action", "illegal", "approve kyc", "otp request", "वर्जित", "ನಿಷಿದ್ಧ")
HUMAN = ("human action", "staff", "operator", "reviewer", "undo", "kill switch", "approved by")
WAIVER = ("waiver", "waived", "maafi", "माफ", "ವಿನಾಯಿತಿ")

_DAYSQL = "substr(ts_sim, 1, 10) = ?"


class NotAllowed(PermissionError):
    pass


class RefusedQuestion(ValueError):
    pass


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    groups: tuple[tuple[str, ...], ...]      # every group must have a hit for the template to match
    sql: str                                 # a fixed SELECT; ``?`` placeholders only
    needs: tuple[str, ...]                   # which extracted parameters it binds, in order


T = (
    Template("loan_refusals", "Every loan-related refusal", (LOAN, REFUSED),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'POLICY' AND json_extract(payload_json, '$.allow') = 0 AND " + _DAYSQL +
             " AND EXISTS (SELECT 1 FROM json_each(ledger.payload_json, '$.rule_ids') WHERE value LIKE 'LOAN-%') ORDER BY seq", ("day",)),
    Template("rule_refusals", "Refusals by one rule", (RULE_WORDS, REFUSED),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'POLICY' AND json_extract(payload_json, '$.allow') = 0 AND " + _DAYSQL +
             " AND EXISTS (SELECT 1 FROM json_each(ledger.payload_json, '$.rule_ids') WHERE value = ?) ORDER BY seq", ("day", "rule")),
    Template("forbidden_attempts", "Attempts at forbidden or unknown actions", (FORBIDDEN,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'POLICY' AND json_extract(payload_json, '$.allow') = 0 AND " + _DAYSQL +
             " AND EXISTS (SELECT 1 FROM json_each(ledger.payload_json, '$.rule_ids') WHERE value IN ('REGISTRY', 'UNKNOWN-ACTION')) ORDER BY seq", ("day",)),
    Template("refusals", "Every refused action", (REFUSED,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'POLICY' AND json_extract(payload_json, '$.allow') = 0 AND " + _DAYSQL +
             " ORDER BY seq", ("day",)),
    Template("escalations", "Every escalation", (ESCALATE,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'ESCALATION' AND " + _DAYSQL + " ORDER BY seq", ("day",)),
    Template("blocked_claims", "Every reply with a blocked claim", (CLAIM,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'VERIFY' AND " + _DAYSQL +
             " AND EXISTS (SELECT 1 FROM json_each(ledger.payload_json, '$.claims') WHERE json_extract(value, '$.color') = 'red') ORDER BY seq", ("day",)),
    Template("waivers", "Fee waivers that were applied", (WAIVER,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'ACTION_RESULT' AND json_extract(payload_json, '$.action') = 'apply_fee_waiver' AND " + _DAYSQL +
             " AND json_extract(payload_json, '$.status') = 'OK' ORDER BY seq", ("day",)),
    Template("human_actions", "Every action by a person", (HUMAN,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind = 'HUMAN_ACTION' AND " + _DAYSQL + " ORDER BY seq", ("day",)),
    Template("case_actions", "Everything Munim did on one case", (ACTION,),
             "SELECT seq, case_id, ts_sim, kind, payload_json FROM ledger WHERE kind IN ('ACTION_INTENT', 'ACTION_RESULT', 'COMPENSATION') AND case_id = ? ORDER BY seq", ("case",)),
)
TEMPLATES = {t.id: t for t in T}


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.lower()).strip()


def match_template(question: str) -> Template | None:
    q = _norm(question)
    best, score = None, 0
    for t in T:
        if all(any(w in q for w in g) for g in t.groups):
            s = len(t.groups) * 10 + sum(len(w) for g in t.groups for w in g if w in q)
            if s > score:
                best, score = t, s
    return best


def _params(question: str, clock) -> dict:
    q = _norm(question)
    today = clock.now().date()
    day = today
    if "yesterday" in q:                                      # ("kal" means both yesterday and tomorrow in Hindi, so it is not read as a day)
        day = today - timedelta(days=1)
    if (m := _DAY.search(q)):
        day = m.group(1)
    case = _CASE.search(question)
    rule = _RULE.search(question)
    return {"day": day.isoformat() if hasattr(day, "isoformat") else day, "case": case.group(1).upper() if case else None, "rule": rule.group(1) if rule else None}


def _readonly(conn: sqlite3.Connection) -> sqlite3.Connection:
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row and row[2] else None
    if path:
        ro = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
    else:                                                       # an in-memory database: same connection, made query-only
        ro = conn
    ro.row_factory = sqlite3.Row
    ro.execute("PRAGMA query_only=ON")
    return ro


def _summary(kind: str, p: dict) -> str:
    if kind == "POLICY":
        return f"{p.get('action')} refused by {', '.join(p.get('rule_ids') or [])} ({p.get('outcome') or 'BLOCK'})"
    if kind in ("ACTION_RESULT", "ACTION_INTENT"):
        return f"{p.get('action')} {p.get('status') or 'intent'}"
    if kind == "ESCALATION":
        return "escalated: " + "; ".join(str(r)[:80] for r in (p.get("reasons") or []))
    if kind == "VERIFY":
        red = [c.get("id") for c in p.get("claims", []) if c.get("color") == "red"]
        return f"reply {p.get('status')}; blocked claims {red}"
    if kind == "HUMAN_ACTION":
        return str(p.get("op") or p.get("action") or "human action")
    return kind


def ask(conn: sqlite3.Connection, clock, question: str, role: str) -> dict:
    if role != AUDIT_ROLE:
        raise NotAllowed(f"only the {AUDIT_ROLE} role may question the ledger")
    if not question or len(question) > 300:
        raise RefusedQuestion("EMPTY_OR_TOO_LONG")
    if _SQLISH.search(question):
        raise RefusedQuestion("SQL_NOT_ACCEPTED")               # nothing is matched, nothing is run
    t = match_template(question)
    if t is None:
        return {"matched": None, "rows": [], "can_ask": [{"id": x.id, "title": x.title} for x in T]}
    params = _params(question, clock)
    missing = [n for n in t.needs if params.get(n) is None]
    if missing:
        return {"matched": t.id, "title": t.title, "rows": [], "needs": missing, "can_ask": [{"id": t.id, "title": t.title}]}
    ro = _readonly(conn)
    try:
        rows = ro.execute(t.sql + f" LIMIT {MAX_ROWS}", [params[n] for n in t.needs]).fetchall()
    finally:
        if ro is not conn:
            ro.close()
    out = [{"seq": r["seq"], "case_id": r["case_id"], "ts_sim": r["ts_sim"], "kind": r["kind"], "summary": _summary(r["kind"], json.loads(r["payload_json"])),
            "link": f"/api/ledger?after_seq={r['seq'] - 1}&limit=1"} for r in rows]
    return {"matched": t.id, "title": t.title, "params": {n: params[n] for n in t.needs}, "count": len(out), "rows": out,
            "truncated": len(out) >= MAX_ROWS}
