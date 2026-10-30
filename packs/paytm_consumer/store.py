"""Consumer pack data (#89): its own tables next to the core schema, and a SYNTHETIC seed. Imports nothing from the merchant pack.

The engine's case subject column is ``cases.merchant_id``; in this pack it holds a CONSUMER id (U-xxxx). Every action's subject argument
is likewise named ``merchant_id`` because that is the engine's subject field, not because the subject is a merchant.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from core.engine.clock import SimClock, iso
from core.memory.store import connect, init_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS consumers (consumer_id TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL, vpa TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consumer_txns (
  txn_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, payee TEXT NOT NULL, amount_paise INTEGER NOT NULL,
  status TEXT NOT NULL,                       -- SUCCESS | DEBITED_PENDING (money left, payee not credited) | REVERSED
  ts TEXT NOT NULL, rrn TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_disputes (
  dispute_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, txn_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_refunds (
  refund_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, txn_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
  status TEXT NOT NULL,                       -- INITIATED | CREDITED
  initiated_at TEXT NOT NULL, expected_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_messages (
  message_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, channel TEXT NOT NULL, body TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_idem (key TEXT PRIMARY KEY, status INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consumer_seq (prefix TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    conn.execute("INSERT INTO consumer_seq(prefix, n) VALUES (?, 1) ON CONFLICT(prefix) DO UPDATE SET n = n + 1", (prefix,))
    return f"{prefix}-{conn.execute('SELECT n FROM consumer_seq WHERE prefix = ?', (prefix,)).fetchone()[0]:04d}"


CONSUMERS = [("U-0001", "SIMULATED Consumer One", "hinglish"), ("U-0002", "SIMULATED Consumer Two", "hi-IN"),
             ("U-0003", "SIMULATED Consumer Three", "kn-IN"), ("U-0004", "SIMULATED Consumer Four", "en-IN"),
             ("U-0005", "SIMULATED Consumer Five", "hinglish")]


def seed(path, clock: SimClock | None = None) -> None:
    """A fresh consumer-pack database: the core schema, this pack's tables, five synthetic consumers with one stuck debit and one refund
    each where the scenarios need them. Deterministic."""
    clock = clock or SimClock()
    now = clock.now()
    conn = connect(path)
    init_schema(conn)
    ensure_schema(conn)
    for cid, name, lang in CONSUMERS:
        conn.execute("INSERT INTO consumers VALUES (?,?,?,?)", (cid, name, lang, f"{cid.lower()}@simulated"))
        conn.execute("INSERT OR IGNORE INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'app', 'GRANTED', 'seed', ?)", (cid, iso(now)))
    rows = [("T-0001", "U-0001", "SIMULATED Kirana", 125000, "DEBITED_PENDING", now - timedelta(hours=3), "400011112222"),
            ("T-0002", "U-0002", "SIMULATED Chemist", 49900, "DEBITED_PENDING", now - timedelta(hours=30), "400011113333"),
            ("T-0003", "U-0003", "SIMULATED Cafe", 26000, "SUCCESS", now - timedelta(days=6), "400011114444"),
            ("T-0004", "U-0004", "SIMULATED Store", 1500000, "DEBITED_PENDING", now - timedelta(hours=2), "400011115555"),
            ("T-0005", "U-0005", "SIMULATED Kirana", 8000, "SUCCESS", now - timedelta(days=10), "400011116666")]
    for t in rows:
        conn.execute("INSERT INTO consumer_txns VALUES (?,?,?,?,?,?,?)", (*t[:5], iso(t[5]), t[6]))
    conn.execute("INSERT INTO consumer_refunds VALUES ('RF-0001', 'U-0003', 'T-0003', 26000, 'INITIATED', ?, ?)",
                 (iso(now - timedelta(days=5)), iso(now + timedelta(days=2))))
    conn.execute("INSERT INTO consumer_refunds VALUES ('RF-0002', 'U-0005', 'T-0005', 8000, 'INITIATED', ?, ?)",
                 (iso(now - timedelta(days=9)), iso(now - timedelta(days=2))))
    conn.close()
