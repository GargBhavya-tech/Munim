"""Event dispatcher: turns rows in the event log into engine work.

For each new event it (1) triages it to a playbook, (2) links to the merchant's open case of that playbook or,
if the event is an OPENING trigger for the playbook, opens one, and (3) wakes the merchant's other open cases so
they re-diagnose against live state (cheap; the engine acts at most once per step per event). The cursor lives in
``meta`` so a restart resumes where it stopped. Plain code: no model, no side effects of its own.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from core.engine.intents import INTENT_TO_PLAYBOOK, normalise_text, top_intent
from core.engine.triage import UNSUPPORTED_PLAYBOOK, OpenCase, triage_event
from core.engine.workflow import MERCHANT_INITIATED, Engine, Event, EventResult

CURSOR_KEY = "dispatch_cursor"
GREETING_PLAYBOOK = "G0_GREETING"
# A message that is ONLY one of these is a greeting. Exact match on the normalised text, never a substring: "hi my settlement has not come" is a request.
GREETINGS = frozenset({"hi", "hii", "hiii", "hello", "helo", "hey", "hey there", "hello there", "good morning", "good afternoon", "good evening",
                       "namaste", "namaskar", "namaskara", "pranam", "नमस्ते", "नमस्कार", "ನಮಸ್ಕಾರ", "ನಮಸ್ತೆ"})


def is_greeting(text: str) -> bool:
    return normalise_text(text or "").strip() in GREETINGS


@dataclass(frozen=True)
class Dispatched:
    event_id: str
    case_id: str
    result: EventResult
    opened: bool


class Dispatcher:
    def __init__(self, conn: sqlite3.Connection, engine: Engine, playbooks: dict, *, message_fallback: str = UNSUPPORTED_PLAYBOOK):
        self.conn, self.engine, self.playbooks = conn, engine, playbooks
        self.message_fallback = message_fallback

    def _cursor(self) -> int:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
        return int(r[0]) if r else 0

    def _set_cursor(self, seq: int) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (CURSOR_KEY, str(seq)))

    def _message_playbook(self, payload: dict) -> str | None:
        """A message from a merchant with no open case opens the case of its intent's playbook; an intent with no playbook of its
        own (unknown, or a control intent such as a language switch) opens the unsupported catch-all, which answers honestly."""
        text = str(payload.get("text") or "")
        intent = payload.get("intent") or (top_intent(text) if text.strip() else None)
        pid = INTENT_TO_PLAYBOOK.get(intent or "")
        if pid in self.playbooks:
            return pid
        if not intent and is_greeting(text) and GREETING_PLAYBOOK in self.playbooks:
            return GREETING_PLAYBOOK                   # a bare greeting is greeted, not refused and handed over
        return self.message_fallback if self.message_fallback in self.playbooks else None

    def _new_topic_playbook(self, payload: dict, open_cases: list) -> str | None:
        """The playbook for a message whose merchant has only cases a person owns (ESCALATED), when the message is about something none of
        those cases is about. Otherwise None: the message stays with the case it belongs to. A greeting, an unsupported message or the topic a
        person already holds never opens a case here, and neither does a merchant who still has a case the engine is working (the model
        raises a second topic itself)."""
        if not open_cases or any(c.state != "ESCALATED" for c in open_cases):
            return None
        pid = self._message_playbook(payload)
        if pid is None or pid in (self.message_fallback, GREETING_PLAYBOOK) or any(c.playbook_id == pid for c in open_cases):
            return None
        return pid

    def _open_cases(self, merchant_id: str) -> list:
        return self.engine.repo.open_cases(merchant_id)

    def run(self) -> list[Dispatched]:
        out: list[Dispatched] = []
        rows = self.conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (self._cursor(),)).fetchall()
        for r in rows:
            out.extend(self._one(r))
            self._set_cursor(r["seq"])
        return out

    def _one(self, r: sqlite3.Row) -> list[Dispatched]:
        merchant_id = r["merchant_id"]
        if not merchant_id or r["case_id"]:
            return []  # lead events and events already bound to a case are not the dispatcher's business
        payload = json.loads(r["payload_json"])
        open_cases = self._open_cases(merchant_id)
        refs = [OpenCase(c.case_id, c.merchant_id, c.playbook_id, c.state) for c in open_cases]
        t = triage_event({"type": r["type"], "merchant_id": merchant_id}, self.playbooks, refs)
        targets: list[tuple[str, bool]] = []
        if t.playbook_id and t.link_case_id:
            targets.append((t.link_case_id, False))
        elif t.playbook_id and r["type"] in self.playbooks[t.playbook_id].triggers.events:
            case = self.engine.open_case(merchant_id, t.playbook_id)
            targets.append((case.case_id, True))
        elif r["type"] in MERCHANT_INITIATED and not open_cases:
            pid = self._message_playbook(payload)
            if pid is not None:
                case = self.engine.open_case(merchant_id, pid, language=payload.get("language"))
                targets.append((case.case_id, True))
        elif r["type"] in MERCHANT_INITIATED and (pid := self._new_topic_playbook(payload, open_cases)) is not None:
            case = self.engine.open_case(merchant_id, pid, language=payload.get("language"))
            targets.append((case.case_id, True))
            open_cases = []                            # the cases a person owns are not woken: they would only repeat the hand-over line
        for c in open_cases:
            if all(c.case_id != tid for tid, _ in targets):
                targets.append((c.case_id, False))
        done = []
        for n, (case_id, opened) in enumerate(targets):
            event_id = r["event_id"] if n == 0 else f"{r['event_id']}@{case_id}"
            res = self.engine.handle_event(Event(event_id, r["type"], merchant_id, case_id, payload))
            done.append(Dispatched(event_id, case_id, res, opened))
        return done
