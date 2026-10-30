"""Lender-collections pack data: its own tables next to the core schema and a SYNTHETIC seed. Imports nothing from any other pack.

The engine's case subject column is ``cases.merchant_id``; in this pack it holds a BORROWER id (B-xxxx). Every action's subject argument
is likewise named ``merchant_id`` because that is the engine's subject field. All people, lenders and amounts are SIMULATED.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from core.engine.clock import SimClock, iso
from core.memory.store import connect, init_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS borrowers (borrower_id TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS loans (
  loan_id TEXT PRIMARY KEY, borrower_id TEXT NOT NULL,
  status TEXT NOT NULL,                      -- DUE_SOON | OVERDUE | PAID
  due_amount_paise INTEGER NOT NULL, due_date TEXT NOT NULL,   -- YYYY-MM-DD, IST
  in_dispute INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promises (
  promise_id TEXT PRIMARY KEY, loan_id TEXT NOT NULL, promised_on TEXT NOT NULL, amount_paise INTEGER NOT NULL,
  status TEXT NOT NULL,                      -- ACTIVE | KEPT | CANCELLED
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lender_messages (
  message_id TEXT PRIMARY KEY, borrower_id TEXT NOT NULL, channel TEXT NOT NULL, body TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lender_idem (key TEXT PRIMARY KEY, status INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lender_seq (prefix TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    conn.execute("INSERT INTO lender_seq(prefix, n) VALUES (?, 1) ON CONFLICT(prefix) DO UPDATE SET n = n + 1", (prefix,))
    return f"{prefix}-{conn.execute('SELECT n FROM lender_seq WHERE prefix = ?', (prefix,)).fetchone()[0]:04d}"


BORROWERS = [("B-0001", "SIMULATED Borrower One", "hinglish"), ("B-0002", "SIMULATED Borrower Two", "hi-IN"),
             ("B-0003", "SIMULATED Borrower Three", "kn-IN"), ("B-0004", "SIMULATED Borrower Four", "en-IN"),
             ("B-0005", "SIMULATED Borrower Five", "hinglish"), ("B-0006", "SIMULATED Borrower Six", "hinglish")]
# (loan, borrower, status, due amount in paise, due date offset in days from the sim start, in_dispute)
LOANS = [("LN-0001", "B-0001", "OVERDUE", 125000, -5, 0), ("LN-0002", "B-0002", "DUE_SOON", 84000, 2, 0),
         ("LN-0003", "B-0003", "OVERDUE", 310000, -12, 0), ("LN-0004", "B-0004", "OVERDUE", 560000, -95, 0),
         ("LN-0005", "B-0005", "OVERDUE", 99000, -8, 1), ("LN-0006", "B-0006", "OVERDUE", 205000, -3, 0)]


def seed(path, clock: SimClock | None = None) -> None:
    """A fresh lender-pack database: the core schema, this pack's tables, six synthetic borrowers (consent on the 'app' channel). Deterministic."""
    clock = clock or SimClock()
    now = clock.now()
    conn = connect(path)
    init_schema(conn)
    ensure_schema(conn)
    for bid, name, lang in BORROWERS:
        conn.execute("INSERT INTO borrowers VALUES (?,?,?)", (bid, name, lang))
        conn.execute("INSERT OR IGNORE INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'app', 'GRANTED', 'seed', ?)", (bid, iso(now)))
    for lid, bid, st, amt, off, disp in LOANS:
        conn.execute("INSERT INTO loans VALUES (?,?,?,?,?,?)", (lid, bid, st, amt, (now + timedelta(days=off)).date().isoformat(), disp))
    conn.close()
