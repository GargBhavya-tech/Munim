"""SQLite access. One place that knows about WAL, autocommit and explicit transactions."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves so BEGIN IMMEDIATE is explicit.
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT. Joins an existing transaction instead of nesting."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_id(conn: sqlite3.Connection, prefix: str, width: int = 4) -> str:
    """Deterministic sequential ids (``W-0001``). Must run inside a write transaction."""
    conn.execute(
        "INSERT INTO counters(name, n) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET n = n + 1", (prefix,)
    )
    n = conn.execute("SELECT n FROM counters WHERE name = ?", (prefix,)).fetchone()[0]
    return f"{prefix}-{n:0{width}d}"


def copy_db(src: str | Path, dst: str | Path) -> None:
    """Consistent copy via the backup API (safe with WAL)."""
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
