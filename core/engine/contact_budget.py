"""Per-merchant contact budget (Bible section 14, "Final contact budget"). Values are [ASSUMPTION].

Proactive touches are stored with direction ``OUT``; merchant-initiated traffic (``IN``) and replies
(``REPLY``) are stored but never counted. Counting is per SUBJECT (merchant or lead) across all cases.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from core.engine.clock import SimClock, iso, parse_iso

CHANNELS = ("app", "soundbox", "whatsapp", "sms", "voice", "email")
WEEKLY_CAP = 2
PER_CHANNEL_CAP = 3
MIN_GAP = timedelta(hours=24)
WINDOW = timedelta(days=7)
QUIET_START_H, QUIET_END_H = 21, 9


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class ContactBudget:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, timers=None):
        self.conn, self.clock, self.timers = conn, clock, timers

    # -- rules ---------------------------------------------------------------------
    def _window(self, subject_id: str):
        r = self.conn.execute("SELECT contact_window_start s, contact_window_end e FROM merchants WHERE merchant_id = ?", (subject_id,)).fetchone()
        return (r["s"], r["e"]) if r and r["s"] and r["e"] else None

    def _in_hours(self, subject_id: str) -> tuple[bool, str | None]:
        now = self.clock.now()
        minutes = now.hour * 60 + now.minute
        w = self._window(subject_id)
        if w:
            s, e = _hm(w[0]), _hm(w[1])
            inside = s <= minutes < e if s <= e else (minutes >= s or minutes < e)
            return (inside, None if inside else "OUTSIDE_STATED_WINDOW")
        quiet = minutes >= QUIET_START_H * 60 or minutes < QUIET_END_H * 60
        return (not quiet, "QUIET_HOURS" if quiet else None)

    def _proactive_touches(self, subject_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM contact_touches WHERE subject_id = ? AND direction = 'OUT' ORDER BY ts, touch_id", (subject_id,)).fetchall()

    def check(self, subject_id: str, channel: str, *, proactive: bool, case_id: str | None = None) -> tuple[bool, str | None]:
        if not proactive:
            return True, None  # the merchant started it: always answered
        rev = self.conn.execute("SELECT 1 FROM consent WHERE subject_id = ? AND status = 'REVOKED' AND (channel = ? OR channel = '*')",
                                (subject_id, channel)).fetchone()
        if rev:
            return False, "STOPPED"
        ok, why = self._in_hours(subject_id)
        if not ok:
            return False, why
        now = self.clock.now()
        touches = self._proactive_touches(subject_id)  # per merchant, never per case
        recent = [t for t in touches if parse_iso(t["ts"]) > now - WINDOW]
        if len(recent) >= WEEKLY_CAP:
            return False, "WEEKLY_CAP"
        if touches and now - parse_iso(touches[-1]["ts"]) < MIN_GAP:
            return False, "COOLING_OFF"
        scope = [t for t in touches if t["channel"] == channel and (case_id is None or t["case_id"] == case_id)]
        if len(scope) >= PER_CHANNEL_CAP:
            return False, "CHANNEL_CAP"
        return True, None

    def record(self, subject_id: str, channel: str, *, case_id: str | None, direction: str = "OUT", proactive: bool = True) -> None:
        if direction == "OUT" and not proactive:
            direction = "REPLY"
        self.conn.execute("INSERT INTO contact_touches(subject_id, channel, case_id, direction, ts) VALUES (?,?,?,?,?)",
                          (subject_id, channel, case_id, direction, self.clock.now_iso()))

    # -- hard stop -----------------------------------------------------------------
    def record_stop(self, subject_id: str, source: str = "stop_message") -> int:
        """"no"/"stop": revoke every channel and cancel all pending follow-ups for the subject."""
        ts = self.clock.now_iso()
        for ch in CHANNELS:
            self.conn.execute(
                "INSERT INTO consent(subject_id, channel, status, source, ts) VALUES (?,?, 'REVOKED', ?, ?) "
                "ON CONFLICT(subject_id, channel) DO UPDATE SET status='REVOKED', source=excluded.source, ts=excluded.ts",
                (subject_id, ch, source, ts))
        return self.timers.cancel_for_subject(subject_id, reason="do_not_contact") if self.timers else 0
