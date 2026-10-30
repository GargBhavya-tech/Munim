"""Judge-session and whole-run evidence export (#136). Thin wrappers: the pack logic lives in core/audit/session_pack.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from core.audit import session_pack as sp
from core.engine.clock import SimClock
from core.judge import scoreboard as sb
from core.judge.session import JudgeSession

DEFAULT_PLAYBOOK_DIR = Path(__file__).resolve().parents[2] / "packs" / "paytm_merchant" / "playbooks"


def export_session(conn: sqlite3.Connection, clock: SimClock, s: JudgeSession, playbook_dir=DEFAULT_PLAYBOOK_DIR) -> dict:
    """The window is the session's start entry through the current head; the scoreboard is the live one, computed from the same ledger."""
    return sp.build_window_pack(conn, clock, start_seq=s.start_seq, scoreboard=sb.compute(conn, s), session=s.as_json(),
                                playbook_dir=playbook_dir, title=f"Judge session {s.session_id}")


def export_run(conn: sqlite3.Connection, clock: SimClock, playbook_dir=DEFAULT_PLAYBOOK_DIR) -> dict:
    return sp.build_window_pack(conn, clock, start_seq=1, scoreboard=None, session=None, playbook_dir=playbook_dir, title="Whole demo run")
