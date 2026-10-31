"""On-call helpers: rung two of the fix ladder, a person who guides the merchant by voice or video.

A fault that Munim cannot clear remotely, but that does not need someone to drive over, is guided by an on-call human helper: they speak the merchant's language,
are on call at that moment, offer the mode asked for (voice, or video when the merchant should show the device), and are the least loaded of those who qualify.
The choice is by code from the roster; it is never a model's pick, and a helper nobody qualifies for is reported with the reason per person.

What this module does NOT do: place the call. Booking goes through the executor (``book_visit_or_call`` with ``kind="call"``), and the call itself is a person's.
The roster is SYNTHETIC. The tables are created here (``CREATE TABLE IF NOT EXISTS``); reads are safe on a read-only connection when the table is not there yet.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from field.shop_hours import to_minutes

SCHEMA = ("CREATE TABLE IF NOT EXISTS helpers (helper_id TEXT PRIMARY KEY, name TEXT NOT NULL, languages_json TEXT NOT NULL, modes_json TEXT NOT NULL, "
          "on_call_start TEXT NOT NULL, on_call_end TEXT NOT NULL, skills_json TEXT NOT NULL)")
MODES = ("voice", "video")
LEVEL = {"basic": 1, "fluent": 2, "native": 3}
MIN_LEVEL = LEVEL["fluent"]
LANGUAGE_NEEDS = {"hinglish": ("hi-IN", "en-IN")}          # a Hinglish speaker is served by anyone fluent in Hindi or English

ROSTER = [                                                  # SYNTHETIC: (id, name, languages, modes, on call from, on call to, skills)
    ("H-01", "Asha", {"hi-IN": "native", "en-IN": "fluent"}, ["voice", "video"], "09:00", "21:00", ["soundbox", "qr"]),
    ("H-02", "Ravi", {"kn-IN": "native", "hi-IN": "fluent"}, ["voice", "video"], "08:00", "20:00", ["soundbox", "settlement"]),
    ("H-03", "Sneha", {"en-IN": "native", "hi-IN": "fluent"}, ["voice"], "10:00", "22:00", ["soundbox", "qr", "settlement"]),
    ("H-04", "Karthik", {"kn-IN": "native", "en-IN": "fluent"}, ["voice", "video"], "14:00", "23:00", ["soundbox"]),
]


@dataclass(frozen=True)
class Helper:
    helper_id: str
    name: str
    languages: dict
    modes: tuple[str, ...]
    on_call: tuple[int, int]
    skills: frozenset

    def level(self, language: str) -> int:
        needs = LANGUAGE_NEEDS.get(language, (language,))
        return max((LEVEL.get(self.languages.get(n, ""), 0) for n in needs), default=0)

    def on_call_at(self, when: datetime) -> bool:
        minute = when.hour * 60 + when.minute
        return self.on_call[0] <= minute < self.on_call[1]


@dataclass(frozen=True)
class HelperChoice:
    helper_id: str
    name: str
    mode: str
    language_level: str
    load: int
    why: tuple[str, ...]


@dataclass
class HelperResult:
    choice: HelperChoice | None
    rejected: dict = field(default_factory=dict)            # helper id -> the first rule they failed


def ensure_roster(conn: sqlite3.Connection) -> None:
    """Idempotent: creates the table and adds the synthetic roster (never changes a helper already there)."""
    conn.execute(SCHEMA)
    for hid, name, langs, modes, start, end, skills in ROSTER:
        conn.execute("INSERT OR IGNORE INTO helpers VALUES (?,?,?,?,?,?,?)", (hid, name, json.dumps(langs), json.dumps(modes), start, end, json.dumps(skills)))


def load_helpers(conn: sqlite3.Connection) -> list[Helper]:
    try:
        rows = conn.execute("SELECT * FROM helpers ORDER BY helper_id").fetchall()
    except sqlite3.OperationalError:                        # no roster yet
        return []
    return [Helper(r["helper_id"], r["name"], json.loads(r["languages_json"]), tuple(json.loads(r["modes_json"])),
                   (to_minutes(r["on_call_start"]), to_minutes(r["on_call_end"])), frozenset(json.loads(r["skills_json"]))) for r in rows]


def _load(conn: sqlite3.Connection, helper_id: str, day: str) -> int:
    """Calls already booked with this helper on that day (cancelled ones do not count)."""
    try:
        return conn.execute("SELECT COUNT(*) FROM bookings WHERE person_id = ? AND kind = 'call' AND status != 'CANCELLED' AND slot_at LIKE ?",
                            (helper_id, f"{day}%")).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def choose_helper(conn: sqlite3.Connection, language: str, at: datetime, *, mode: str = "voice", skill: str = "soundbox") -> HelperResult:
    """The helper who may take this call: speaks the language (fluent or better), is on call at ``at``, offers ``mode`` and knows ``skill``.
    Among those: the better language level first, then the lighter day, then the id (so the answer is the same every time)."""
    if mode not in MODES:
        raise ValueError(f"mode is one of {MODES}")
    day = at.date().isoformat()
    ok, rejected = [], {}
    for h in load_helpers(conn):
        if h.level(language) < MIN_LEVEL:
            rejected[h.helper_id] = "LANGUAGE"
        elif not h.on_call_at(at):
            rejected[h.helper_id] = "NOT_ON_CALL"
        elif mode not in h.modes:
            rejected[h.helper_id] = "MODE"
        elif skill not in h.skills:
            rejected[h.helper_id] = "SKILL"
        else:
            ok.append((-h.level(language), _load(conn, h.helper_id, day), h.helper_id, h))
    if not ok:
        return HelperResult(None, rejected)
    _, load, _, h = min(ok, key=lambda t: t[:3])
    names = {v: k for k, v in LEVEL.items()}
    why = (f"speaks {language} ({names[h.level(language)]})", f"on call until {h.on_call[1] // 60:02d}:{h.on_call[1] % 60:02d}", f"offers {mode}", f"{load} calls booked that day")
    return HelperResult(HelperChoice(h.helper_id, h.name, mode, names[h.level(language)], load, why), rejected)
