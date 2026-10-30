"""Persona cards, exact DB snapshots and reset-to-snapshot (#128).

Reset restores a copy of the SQLite file through the backup API and nothing else: no executor, no
mock, no clock. Because the whole file is replaced, outbox, timers, ledger, idempotency keys,
circuit-breaker state and AUTOINCREMENT counters all return to the snapshot (the "Watch out").
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.memory.store import connect


class PersonaMismatch(Exception):
    """A snapshot belongs to a different persona than the session it was restored into."""


@dataclass(frozen=True)
class PersonaCard:
    persona_id: str
    title: str
    merchant_id: str
    description: str


@dataclass(frozen=True)
class Snapshot:
    persona_id: str
    merchant_id: str
    state_hash: str
    path: Path


def list_personas(conn: sqlite3.Connection) -> list[PersonaCard]:
    rows = conn.execute("SELECT persona_id, title, merchant_id, description FROM personas ORDER BY persona_id")
    return [PersonaCard(r[0], r[1], r[2], r[3] or "") for r in rows]


def get_persona(conn: sqlite3.Connection, persona_id: str) -> PersonaCard:
    for p in list_personas(conn):
        if p.persona_id == persona_id:
            return p
    raise KeyError(f"unknown persona {persona_id}")


def _cell(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"b": bytes(v).hex()}
    if isinstance(v, float):
        return {"f": repr(v)}
    return v


def state_hash(conn: sqlite3.Connection) -> str:
    """sha256 over every table (schema name + all rows, order independent, typed cells).

    Includes sqlite_sequence (next AUTOINCREMENT id changes behaviour). Skips only the FTS5
    virtual table and its shadow tables: they are derived from kb_chunks, which is hashed.
    """
    names = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if not r[0].startswith("kb_chunks_fts")
    ]
    h = hashlib.sha256()
    for name in names:
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{name}")')]
        rows = sorted(
            json.dumps([_cell(v) for v in row], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for row in conn.execute(f'SELECT * FROM "{name}"')
        )
        h.update(json.dumps({"t": name, "cols": cols, "n": len(rows)}, sort_keys=True).encode("utf-8"))
        for r in rows:
            h.update(b"\n" + r.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def take_snapshot(db_path: str | Path, persona_id: str, dest: str | Path) -> Snapshot:
    """Consistent copy of the DB for ``persona_id`` plus its state hash."""
    db_path, dest = Path(db_path), Path(dest)
    src = connect(db_path)
    try:
        card = get_persona(src, persona_id)
        sh = state_hash(src)
    finally:
        src.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(dest) + suffix).unlink(missing_ok=True)
    s, d = sqlite3.connect(str(db_path)), sqlite3.connect(str(dest))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    snap = Snapshot(persona_id, card.merchant_id, sh, dest)
    Path(str(dest) + ".json").write_text(
        json.dumps({"persona_id": persona_id, "merchant_id": card.merchant_id, "state_hash": sh}, sort_keys=True),
        encoding="utf-8",
    )
    return snap


class PersonaSession:
    """A live DB bound to one persona. Only restores snapshots of that same persona."""

    def __init__(self, db_path: str | Path, persona_id: str):
        self.db_path = Path(db_path)
        self.persona_id = persona_id
        conn = connect(self.db_path)
        try:
            self.card = get_persona(conn, persona_id)
        finally:
            conn.close()

    def state_hash(self) -> str:
        conn = connect(self.db_path)
        try:
            return state_hash(conn)
        finally:
            conn.close()

    def snapshot(self, dest: str | Path) -> Snapshot:
        return take_snapshot(self.db_path, self.persona_id, dest)

    def reset(self, snap: Snapshot) -> str:
        """Replace the live DB content with the snapshot. Returns the resulting state hash."""
        if snap.persona_id != self.persona_id or snap.merchant_id != self.card.merchant_id:
            raise PersonaMismatch(f"snapshot is for {snap.persona_id}/{snap.merchant_id}, session is {self.persona_id}")
        s = sqlite3.connect(str(snap.path))
        d = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        got = self.state_hash()
        if got != snap.state_hash:
            raise RuntimeError(f"reset not exact: {got} != {snap.state_hash}")
        return got
