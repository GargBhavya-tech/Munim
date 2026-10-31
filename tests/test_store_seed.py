import json

from core.memory.store import connect
from data.seed import seed

TABLES = """merchants kyc bank devices payments settlements offers plans risk_flags tickets bookings comms_outbox refunds closures
lender_apps mandates cases events turns actions idempotency profile_facts episodes kb_docs kb_chunks kb_vectors handoffs ledger
metrics_runs leads sources reps technicians routes timers compensations breaker_state approvals incidents consent contact_touches
mock_idempotency chaos personas dnd_registry case_events links schedules device_replacements device_orders plan_changes offer_bands
meta counters""".split()


def test_every_table_exists_and_wal_is_on(conn):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
    missing = [t for t in TABLES if t not in existing]
    assert missing == []
    assert "kb_chunks_fts" in existing
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_seed_counts(conn):
    assert conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 200
    assert conn.execute("SELECT COUNT(*) FROM reps").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM technicians").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 300
    langs = {r[0] for r in conn.execute("SELECT DISTINCT language FROM merchants")}
    assert langs == {"hi-IN", "kn-IN", "en-IN", "hinglish"}
    segs = {r[0] for r in conn.execute("SELECT DISTINCT segment FROM merchants")}
    assert segs == {"kirana", "restaurant", "chemist", "d2c"}
    cities = {r[0] for r in conn.execute("SELECT DISTINCT city FROM merchants")}
    assert {"Hubballi", "Bengaluru", "Pune"} <= cities


def test_seeding_twice_gives_byte_identical_dumps(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    seed(a, 42)
    seed(b, 42)
    da, db_ = list(connect(a).iterdump()), list(connect(b).iterdump())
    assert da == db_ and len(da) > 1000
    c = tmp_path / "c.db"
    seed(c, 7)
    assert list(connect(c).iterdump()) != da  # the seed matters


def test_ids_are_deterministic_and_sequential(conn):
    ids = [r[0] for r in conn.execute("SELECT merchant_id FROM merchants ORDER BY merchant_id")]
    assert ids[0] == "M-0001" and ids[-1] == "M-0200"


def test_seeded_duplicates_are_recorded_for_the_dedupe_test(conn):
    dupes = json.loads(conn.execute("SELECT value FROM meta WHERE key='seed_duplicates'").fetchone()[0])
    assert len(dupes) == 40
    assert all(v.startswith("M-") for v in dupes.values())


def test_no_protected_trait_columns_anywhere(conn):
    banned = ("religion", "caste", "community")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for col in conn.execute(f"PRAGMA table_info('{name}')"):
            assert not any(b in col["name"].lower() for b in banned), (name, col["name"])


def test_source_registry_marks_scraping_forbidden_sites_manual_only(conn):
    rows = {r["source_id"]: r["access"] for r in conn.execute("SELECT source_id, access FROM sources")}
    for s in ("zomato", "swiggy", "indiamart", "justdial"):
        assert rows[s] == "manual_only"
    assert rows["places_api"] == "official_api"
