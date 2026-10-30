import ast
import shutil
from pathlib import Path

import pytest

from core.judge import personas as P
from core.judge.personas import PersonaMismatch, PersonaSession, list_personas, state_hash
from core.memory.store import connect


def _mutate(path):
    c = connect(path)
    c.execute("INSERT INTO comms_outbox VALUES ('C-X','M-0001','sms','hi-IN','hello',NULL,'QUEUED','2026-01-01T00:00:00')")
    c.execute("INSERT INTO timers(case_id,kind,due_at) VALUES ('W-1','followup','2026-01-02T00:00:00')")
    c.execute("UPDATE merchants SET language='en-IN' WHERE merchant_id='M-0001'")
    c.execute("DELETE FROM kb_vectors WHERE rowid IN (SELECT rowid FROM kb_vectors LIMIT 1)")
    c.close()


def test_six_persona_cards(conn):
    cards = list_personas(conn)
    assert [c.persona_id for c in cards] == ["P-1", "P-2", "P-3", "P-4", "P-5", "P-6"]
    assert cards[0].merchant_id == "M-0001" and "KYC rejected" in cards[0].title


def test_hash_is_deterministic_and_sensitive(db_path, tmp_path):
    c = connect(db_path)
    h1, h2 = state_hash(c), state_hash(c)
    c.close()
    assert h1 == h2 and len(h1) == 64
    other = tmp_path / "o.db"
    shutil.copy(db_path, other)
    c = connect(other)
    assert state_hash(c) == h1  # a copy hashes the same
    c.execute("UPDATE timers SET status='X'") if c.execute("SELECT COUNT(*) FROM timers").fetchone()[0] else c.execute(
        "INSERT INTO timers(case_id,kind,due_at) VALUES ('a','b','c')")
    assert state_hash(c) != h1  # timers are inside the hash
    c.close()


def test_mutate_reset_gives_identical_hash_and_clears_outbox_and_timers(db_path, tmp_path):
    sess = PersonaSession(db_path, "P-1")
    snap = sess.snapshot(tmp_path / "snap" / "p1.db")
    original = sess.state_hash()
    assert original == snap.state_hash
    c = connect(db_path)
    outbox0 = c.execute("SELECT COUNT(*) FROM comms_outbox").fetchone()[0]
    timers0 = c.execute("SELECT COUNT(*) FROM timers").fetchone()[0]
    c.close()
    _mutate(db_path)
    assert sess.state_hash() != original
    assert sess.reset(snap) == original
    assert sess.state_hash() == original
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM comms_outbox").fetchone()[0] == outbox0
    assert c.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == timers0
    assert c.execute("SELECT language FROM merchants WHERE merchant_id='M-0001'").fetchone()[0] != "en-IN"
    c.close()


def test_reset_is_repeatable(db_path, tmp_path):
    sess = PersonaSession(db_path, "P-2")
    snap = sess.snapshot(tmp_path / "p2.db")
    for _ in range(2):
        _mutate(db_path)
        assert sess.reset(snap) == snap.state_hash


def test_snapshot_cannot_restore_over_a_different_persona(db_path, tmp_path):
    s1 = PersonaSession(db_path, "P-1")
    snap1 = s1.snapshot(tmp_path / "p1.db")
    other = tmp_path / "other.db"
    shutil.copy(db_path, other)
    s2 = PersonaSession(other, "P-2")
    _mutate(other)
    before = s2.state_hash()
    with pytest.raises(PersonaMismatch):
        s2.reset(snap1)
    assert s2.state_hash() == before  # refused, DB untouched


def test_unknown_persona_rejected(db_path):
    with pytest.raises(KeyError):
        PersonaSession(db_path, "P-99")


def test_reset_path_has_no_side_effect_imports():
    tree = ast.parse(Path(P.__file__).read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    assert not [m for m in mods if m.startswith(("core.actions", "packs", "core.engine", "requests", "httpx"))]
    assert "time" not in mods and "datetime" not in mods
