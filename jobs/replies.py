"""Reply handling and speed to lead (#103; Bible section 11), plus the NO_REPLY / NOT_NOW retry timers.

Every inbound lead message is classified by CODE (never by the model) into one of:
  unsubscribe | angry | wrong_person | out_of_office | not_now | objection | question | interested | other
The order of that list is the precedence. An unsubscribe is therefore never mistaken for an objection ("too costly, stop calling" is an
unsubscribe), and an angry message is never answered with a pitch.

  unsubscribe   -> DO_NOT_CONTACT at once: the number joins ``dnd_registry``, every channel consent is REVOKED, pending retries and lifecycle timers
                   are cancelled, nothing is sent back. Final.
  angry         -> a person takes it (ESCALATION on the ledger), retries stop, one fixed hand-off line goes out.
  wrong_person  -> DISQUALIFIED, retries stop.
  out_of_office -> NOT_NOW then NURTURE, one retry after ``OUT_OF_OFFICE_DAYS``.
  not_now       -> NOT_NOW then NURTURE, one retry later (``NOT_NOW_DAYS``).
  objection     -> the approved answer section(s) are returned as sources for the gate to render (this module never writes the sentence).
  question      -> approved KB sections ranked by word overlap; if nothing approved answers it, a person does.
  interested    -> ENGAGED; speed to lead: a fixed acknowledgement within ``SLA_SECONDS``, then the turn agent / a person continues.

Retries: ``arm_no_reply`` (called after a first contact really went out) schedules a check; if the lead has not replied by then it becomes
NO_REPLY and a retry is scheduled; at most ``MAX_RETRIES`` re-contacts, each through OutreachJob (consent, DND, budget and policy all apply again).
All timers are rows in ``lead_retries`` armed on the SimClock; a firing re-reads the lead and does nothing if it is no longer in the state the
timer was set for (a reply, an unsubscribe, a disqualification all make it a no-op), so a timer can never contact a lead that has said stop.
Constants are [ASSUMPTION].
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from core.actions.types import ActionIntent
from core.engine.clock import SimClock, iso, parse_iso
from core.engine.events import append_event
from core.engine.intents import detect_intent, normalise_text
from core.memory.knowledge import parse_doc
from core.memory.store import write_txn
from jobs.lead_lifecycle import IllegalTransition, LeadLifecycle
from jobs.outreach import OUTREACH_PLAYBOOK, PRINCIPAL, SENT

CLASSES = ("unsubscribe", "angry", "wrong_person", "out_of_office", "not_now", "objection", "question", "interested", "other")
SLA_SECONDS = 300                       # inbound answered within five minutes
NO_REPLY_AFTER = timedelta(hours=48)
RETRY_AFTER = timedelta(days=3)
NOT_NOW_DAYS = 14
OUT_OF_OFFICE_DAYS = 3
RETRY_HOUR = 10                         # a retry the contact budget refuses (quiet hours, cooling off, weekly cap) is tried again at the next 10:00
MAX_DEFERS = 5
MAX_RETRIES = 2
KB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "kb"
ANSWER_DOCS = ("objection_answers", "fee_schedule", "plans_and_billing", "safety_lines", "soundbox_howto", "kyc_faq")   # approved; loan terms are never answered here
MIN_OVERLAP = 2
ACK_PHRASE = {"interested": "one_moment", "question": "one_moment", "objection": "one_moment", "angry": "handoff", "not_now": "thank_you", "other": "clarify"}

# English and romanised-Hindi cue phrases. Hindi and Kannada script cues come ONLY from the pack's intents.yaml (STOP). Native review expected.
_CUES: dict[str, tuple[str, ...]] = {
    "unsubscribe": ("unsubscribe", "stop", "stop messaging", "stop calling", "do not contact", "dont contact", "remove me", "remove my number", "no more messages",
                    "leave me alone", "do not call", "dont call", "mat bhejo", "message mat karo", "call mat karo", "band karo", "number hata do", "hata do", "bas karo"),
    "angry": ("scam", "fraud", "cheat", "cheating", "spam", "harass", "harassing", "harassment", "worst", "useless", "idiot", "shut up", "police", "consumer court",
              "fake", "bakwas", "chor", "dhokha", "pareshan"),
    "wrong_person": ("wrong number", "wrong person", "not the owner", "not my shop", "not my number", "galat number", "galat banda", "dukaan mera nahi", "sorry wrong"),
    "out_of_office": ("out of office", "out of station", "on leave", "on vacation", "away until", "travelling", "traveling", "bahar hoon", "gaon gaya", "chhutti pe",
                      "shahar se bahar"),
    "not_now": ("not now", "later", "call later", "call me later", "busy", "in a meeting", "next week", "next month", "abhi nahi", "abhi time nahi", "baad mein",
                "baad me", "kal baat", "thoda busy", "abhi busy", "phir kabhi"),
    "objection": ("not interested", "no thanks", "no thank you", "nahi chahiye", "zaroorat nahi", "interested nahi", "too high", "too costly", "too expensive",
                  "expensive", "costly", "mehenga", "mehnga", "already have", "already using", "pehle se hai", "phonepe", "google pay", "gpay", "rent is", "rent too", "kiraya zyada", "kiraya jyada",
                  "dont trust", "do not trust", "bharosa nahi", "charges", "mdr"),
    "interested": ("interested", "tell me more", "send details", "how to start", "how do i start", "i want", "sign me up", "go ahead", "ok go ahead"),
    "yes": ("yes", "haan", "han", "ha", "ji haan", "batao", "bataiye", "chalega"),
}
_QUESTION_WORDS = ("how", "what", "which", "why", "when", "where", "can i", "do you", "is it", "kya", "kitna", "kitni", "kaise", "kab", "kaun", "kahan", "kyun", "?")
_OBJECTION_SECTION = (
    (("charges", "mdr", "expensive", "costly", "too high", "too costly", "mehenga", "mehnga"), "charges_too_high"),
    (("phonepe", "google pay", "gpay", "already have", "already using", "pehle se hai"), "already_using_other"),
    (("rent is", "rent too", "kiraya zyada", "kiraya jyada"), "device_rent"),
    (("dont trust", "do not trust", "bharosa nahi"), "trust"),
)
_STOPWORDS = frozenset("the a an is are of to in on for and or it i you me my your do does can how what which with at be this that kya hai hain ka ki ke se ko".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_replies (
  reply_id TEXT PRIMARY KEY, lead_id TEXT NOT NULL, channel TEXT NOT NULL, cls TEXT NOT NULL, received_at TEXT NOT NULL,
  acked_at TEXT, ack_seconds INTEGER, sla_met INTEGER, action TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_retries (
  retry_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL, kind TEXT NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING', attempt INTEGER NOT NULL DEFAULT 0, defers INTEGER NOT NULL DEFAULT 0, language TEXT NOT NULL DEFAULT 'en-IN', note TEXT
)
"""


def _has(norm: str, cue: str) -> bool:
    return re.search(r"(?<![^\W_])" + re.escape(normalise_text(cue)) + r"(?![^\W_])", norm) is not None


def classify(text: str) -> str:
    """The reply class, by the precedence in the module docstring."""
    norm = normalise_text(text)
    if any(i == "STOP" for i, _ in detect_intent(text)) or any(_has(norm, c) for c in _CUES["unsubscribe"]):
        return "unsubscribe"
    for cls in ("angry", "wrong_person", "out_of_office", "not_now", "objection"):
        if any(_has(norm, c) for c in _CUES[cls]):
            return cls
    if any(_has(norm, c) for c in _CUES["interested"]):
        return "interested"
    if "?" in (text or "") or any(_has(norm, q) for q in _QUESTION_WORDS if q != "?"):
        return "question"
    if any(_has(norm, c) for c in _CUES["yes"]):
        return "interested"
    return "other"


@dataclass(frozen=True)
class KBAnswer:
    source_id: str           # kb:<doc>#<section>@v<version>, the id the grounding gate resolves
    text: str
    overlap: int = 0


def _tokens(s: str) -> set[str]:
    return {t for t in normalise_text(s).split() if t not in _STOPWORDS and len(t) > 2}


_KB_CACHE: dict[str, list[tuple[str, str, str, set[str]]]] = {}


def _kb_sections(kb_dir: Path = KB_DIR) -> list[tuple[str, str, str, set[str]]]:
    key = str(kb_dir)
    if key not in _KB_CACHE:
        out = []
        for doc_id in ANSWER_DOCS:
            p = kb_dir / f"{doc_id}.md"
            if not p.exists():
                continue
            d = parse_doc(p)
            for s in d.sections:
                out.append((f"kb:{d.chunk_id(s.section_id)}", s.section_id, s.text, _tokens(s.heading + " " + s.text)))
        _KB_CACHE[key] = out
    return _KB_CACHE[key]


def objection_answers(text: str) -> list[KBAnswer]:
    norm = normalise_text(text)
    wanted = [sid for cues, sid in _OBJECTION_SECTION if any(_has(norm, c) for c in cues)]
    by_section = {(sid): (src, body) for src, sid, body, _ in _kb_sections() if src.startswith("kb:objection_answers#")}
    return [KBAnswer(by_section[s][0], by_section[s][1]) for s in wanted if s in by_section]


def answer_question(text: str) -> KBAnswer | None:
    """The approved section that best covers the question, or None (a person answers). Needs ``MIN_OVERLAP`` shared content words."""
    q = _tokens(text)
    best = max(((len(q & toks), src, body) for src, _, body, toks in _kb_sections()), default=(0, "", ""), key=lambda t: (t[0], t[1]))
    return KBAnswer(best[1], best[2], best[0]) if best[0] >= MIN_OVERLAP else None


@dataclass
class ReplyResult:
    lead_id: str
    cls: str
    action: str                                  # DO_NOT_CONTACT | HANDOFF | DISQUALIFIED | RETRY_LATER | ENGAGED | ANSWER | OBJECTION_ANSWER | CLARIFY | DUPLICATE
    lead_state: str
    sources: list[KBAnswer] = field(default_factory=list)
    ack_status: str | None = None
    ack_seconds: int | None = None
    sla_met: bool | None = None
    reply_id: str = ""


class ReplyHandler:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, executor, lifecycle: LeadLifecycle, outreach, budget=None, phrases=None):
        self.conn, self.clock, self.ledger, self.executor, self.life, self.outreach, self.budget = conn, clock, ledger, executor, lifecycle, outreach, budget
        self.phrases = phrases or outreach.phrases
        self._handles: dict[int, int] = {}
        outreach.after_contact = self.arm_no_reply
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)

    # ------------------------------------------------------------------------------------------------------ handling a reply
    def handle(self, lead_id: str, text: str, channel: str, received_at=None, language: str = "en-IN") -> ReplyResult:
        received = received_at or self.clock.now()
        rid = "LR-" + hashlib.sha256(f"{lead_id}|{channel}|{iso(received)}|{text}".encode("utf-8")).hexdigest()[:16]
        ins = append_event(self.conn, event_id=rid, type="LEAD_REPLY", payload={"lead_id": lead_id, "channel": channel, "text": text}, ts_sim=iso(received))
        if not ins.inserted:                                            # the same message delivered twice is handled once
            row = self.conn.execute("SELECT * FROM lead_replies WHERE reply_id = ?", (rid,)).fetchone()
            return ReplyResult(lead_id, row["cls"] if row else "other", "DUPLICATE", self._state(lead_id), reply_id=rid)
        cls = classify(text)
        self.cancel_retries(lead_id, "reply")                           # a reply answers any pending no-reply check or retry
        action, sources = getattr(self, f"_on_{cls}")(lead_id, text, language)
        ack_status = ack_seconds = sla = None
        phrase = ACK_PHRASE.get(cls)
        if phrase and action not in ("DO_NOT_CONTACT", "DISQUALIFIED") and not (cls == "question" and not sources) and cls != "out_of_office":
            ack_status = self._ack(lead_id, channel, phrase, language)
            if ack_status in (SENT,):
                ack_seconds = max(0, int((self.clock.now() - received).total_seconds()))
                sla = ack_seconds <= SLA_SECONDS
        with write_txn(self.conn):
            self.conn.execute("INSERT INTO lead_replies(reply_id, lead_id, channel, cls, received_at, acked_at, ack_seconds, sla_met, action) VALUES (?,?,?,?,?,?,?,?,?)",
                              (rid, lead_id, channel, cls, iso(received), self.clock.now_iso() if ack_seconds is not None else None, ack_seconds,
                               None if sla is None else int(sla), action))
            self.ledger.append(kind="STATE", actor="SYSTEM", payload={"job": "replies", "lead_id": lead_id, "reply_id": rid, "class": cls, "action": action,
                                                                       "sla_met": sla})
        return ReplyResult(lead_id, cls, action, self._state(lead_id), sources, ack_status, ack_seconds, sla, rid)

    def _state(self, lead_id: str) -> str:
        return self.conn.execute("SELECT state FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()["state"]

    def _ack(self, lead_id: str, channel: str, phrase: str, language: str) -> str:
        """One fixed approved line, through the executor. A reply is not proactive (no cap, no quiet hours), but consent and DND still apply."""
        body = self.phrases.text(phrase, language)
        n = self.conn.execute("SELECT COUNT(*) FROM lead_replies WHERE lead_id = ?", (lead_id,)).fetchone()[0]
        intent = ActionIntent("send_message", {"merchant_id": lead_id, "channel": channel, "body": body, "language": language}, step_seq=1000 + n,
                              principal=PRINCIPAL, playbook_id=OUTREACH_PLAYBOOK, step="reply", proactive=False, channel=channel,
                              allowlist=("send_message",), facts={"lead_id": lead_id})
        out = self.executor.submit(intent)
        return SENT if out.status in ("OK", "REPLAYED") else out.status

    # -- per class ----------------------------------------------------------------------------------------------------
    def _on_unsubscribe(self, lead_id, text, language):
        try:
            self.life.move(lead_id, "DO_NOT_CONTACT", "unsubscribe")        # DND registry + lifecycle timers cancelled inside
        except IllegalTransition:
            pass                                                           # already DISQUALIFIED / DO_NOT_CONTACT: the stop below still applies
        row = self.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        with write_txn(self.conn):
            if row and row["phone_public"]:
                self.conn.execute("INSERT OR IGNORE INTO dnd_registry(phone) VALUES (?)", (row["phone_public"],))
            if self.budget is not None:
                self.budget.record_stop(lead_id, "lead_unsubscribe")        # every channel REVOKED
        return "DO_NOT_CONTACT", []

    def _on_angry(self, lead_id, text, language):
        self.ledger.append(kind="ESCALATION", actor="SYSTEM", payload={"lead_id": lead_id, "reason": "ANGRY_LEAD", "to": "human"})
        return "HANDOFF", []

    def _on_wrong_person(self, lead_id, text, language):
        try:
            self.life.move(lead_id, "DISQUALIFIED", "wrong person")
        except IllegalTransition:
            pass
        return "DISQUALIFIED", []

    def _on_out_of_office(self, lead_id, text, language):
        return self._park(lead_id, "OUT_OF_OFFICE", timedelta(days=OUT_OF_OFFICE_DAYS), language)

    def _on_not_now(self, lead_id, text, language):
        return self._park(lead_id, "NOT_NOW", timedelta(days=NOT_NOW_DAYS), language)

    def _park(self, lead_id, kind, wait, language):
        """NOT_NOW -> NURTURE with one retry timer. An out-of-office is the same, with a shorter wait."""
        st = self._state(lead_id)
        try:
            if st == "NO_REPLY":
                self.life.move(lead_id, "NURTURE", "not now")
            elif st != "NURTURE":
                self.life.move(lead_id, "NOT_NOW", "not now")
                self.life.move(lead_id, "NURTURE", "retry later")
        except IllegalTransition:
            return "CLARIFY", []
        self._schedule(lead_id, kind, self.clock.now() + wait, attempt=0, language=language)
        return "RETRY_LATER", []

    def _engage(self, lead_id) -> None:
        path = {"CONTACTED": ["ENGAGED"], "NO_REPLY": ["CONTACTED", "ENGAGED"], "NURTURE": ["CONTACTED", "ENGAGED"], "NOT_NOW": ["NURTURE", "CONTACTED", "ENGAGED"]}
        for to in path.get(self._state(lead_id), []):
            self.life.move(lead_id, to, "lead replied")

    def _on_objection(self, lead_id, text, language):
        self._engage(lead_id)
        src = objection_answers(text)
        return ("OBJECTION_ANSWER", src) if src else ("HANDOFF", [])

    def _on_question(self, lead_id, text, language):
        self._engage(lead_id)
        a = answer_question(text)
        return ("ANSWER", [a]) if a else ("HANDOFF", [])

    def _on_interested(self, lead_id, text, language):
        self._engage(lead_id)
        return "ENGAGED", []

    def _on_other(self, lead_id, text, language):
        return "CLARIFY", []

    # ------------------------------------------------------------------------------------------------------ retry timers
    def _schedule(self, lead_id: str, kind: str, due, attempt: int, language: str, defers: int = 0) -> int:
        with write_txn(self.conn):
            cur = self.conn.execute("INSERT INTO lead_retries(lead_id, kind, due_at, status, attempt, defers, language) VALUES (?,?,?, 'PENDING', ?,?,?)",
                                    (lead_id, kind, iso(due), attempt, defers, language))
            rid = cur.lastrowid
        self._arm(rid, due)
        return rid

    def _arm(self, retry_id: int, due) -> None:
        self._handles[retry_id] = self.clock.call_at(due, lambda: self._fire(retry_id))

    def arm_no_reply(self, lead_id: str, language: str = "en-IN") -> int:
        """Called (via OutreachJob.after_contact) after a contact really went out: if the lead is silent by then it becomes NO_REPLY and is retried.
        The attempt number is the count of retries already made; only one silence check is ever pending per lead."""
        self.cancel_retries(lead_id, "re-armed")
        attempt = self.conn.execute("SELECT COUNT(*) FROM lead_retries WHERE lead_id = ? AND kind = 'RETRY' AND status = 'FIRED'", (lead_id,)).fetchone()[0]
        return self._schedule(lead_id, "NO_REPLY_CHECK", self.clock.now() + NO_REPLY_AFTER, attempt, language)

    def rearm(self) -> int:
        """After a restart: re-register every pending retry; overdue ones fire now."""
        n = 0
        for r in self.conn.execute("SELECT retry_id, due_at FROM lead_retries WHERE status = 'PENDING' ORDER BY due_at, retry_id").fetchall():
            if r["retry_id"] in self._handles:
                continue
            due = parse_iso(r["due_at"])
            if due <= self.clock.now():
                self._fire(r["retry_id"])
            else:
                self._arm(r["retry_id"], due)
            n += 1
        return n

    def pending(self, lead_id: str | None = None) -> list[sqlite3.Row]:
        q, a = "SELECT * FROM lead_retries WHERE status = 'PENDING'", []
        if lead_id:
            q, a = q + " AND lead_id = ?", [lead_id]
        return self.conn.execute(q + " ORDER BY retry_id", a).fetchall()

    def cancel_retries(self, lead_id: str, reason: str) -> int:
        with write_txn(self.conn):
            rows = self.conn.execute("SELECT retry_id FROM lead_retries WHERE lead_id = ? AND status = 'PENDING'", (lead_id,)).fetchall()
            for r in rows:
                self._close(r["retry_id"], "CANCELLED", reason)
        return len(rows)

    def _close(self, retry_id: int, status: str, note: str) -> None:
        self.conn.execute("UPDATE lead_retries SET status = ?, note = ? WHERE retry_id = ? AND status = 'PENDING'", (status, note, retry_id))
        h = self._handles.pop(retry_id, None)
        if h is not None:
            self.clock.cancel(h)

    def _fire(self, retry_id: int) -> None:
        self._handles.pop(retry_id, None)
        with write_txn(self.conn):
            r = self.conn.execute("SELECT * FROM lead_retries WHERE retry_id = ? AND status = 'PENDING'", (retry_id,)).fetchone()
            if r is None:
                return                                                    # fired or cancelled already: exactly once
            self.conn.execute("UPDATE lead_retries SET status = 'FIRED' WHERE retry_id = ?", (retry_id,))
        lead_id, kind, attempt, lang = r["lead_id"], r["kind"], r["attempt"], r["language"]
        state = self._state(lead_id)
        if self._must_not_contact(lead_id):
            self._note(retry_id, "stopped: lead said stop or is on DND")
            return
        if kind == "NO_REPLY_CHECK":
            if state != "CONTACTED":
                return                                                    # it replied or moved on: nothing to do
            self.life.move(lead_id, "NO_REPLY", "no reply")
            if attempt < MAX_RETRIES:
                self._schedule(lead_id, "RETRY", self.clock.now() + RETRY_AFTER, attempt + 1, lang)
            else:
                self.life.move(lead_id, "NURTURE", "retries used up")
            return
        if kind in ("RETRY", "NOT_NOW", "OUT_OF_OFFICE"):
            if state not in ("NO_REPLY", "NURTURE"):
                return                                                    # it replied or moved on meanwhile
            self._recontact(retry_id, lead_id, kind, attempt, lang, r["defers"])

    def _recontact(self, retry_id: int, lead_id: str, kind: str, attempt: int, lang: str, defers: int) -> None:
        res = self.outreach.first_contact(lead_id, lang)
        self._note(retry_id, f"recontact: {res.status}")
        if res.status == SENT:
            pass                                                           # OutreachJob.after_contact armed the next silence check
        elif res.status == "BLOCKED" and any(x.startswith("CONTACT-CAP") for x in res.rule_ids) and defers < MAX_DEFERS:
            self._schedule(lead_id, kind, self._next_window(), attempt, lang, defers + 1)

    def _next_window(self):
        """The next 10:00 IST (today if it is earlier than that): inside contact hours, and past a 24-hour cooling-off."""
        now = self.clock.now()
        at = now.replace(hour=RETRY_HOUR, minute=0, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)

    def _note(self, retry_id: int, note: str) -> None:
        with write_txn(self.conn):
            self.conn.execute("UPDATE lead_retries SET note = ? WHERE retry_id = ?", (note, retry_id))

    def _must_not_contact(self, lead_id: str) -> bool:
        if self._state(lead_id) in ("DO_NOT_CONTACT", "DISQUALIFIED"):
            return True
        row = self.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return bool(row and row["phone_public"] and self.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (row["phone_public"],)).fetchone())


__all__ = ["ReplyHandler", "ReplyResult", "classify", "answer_question", "objection_answers", "KBAnswer", "CLASSES", "SLA_SECONDS", "NO_REPLY_AFTER", "RETRY_AFTER",
           "NOT_NOW_DAYS", "MAX_RETRIES"]
