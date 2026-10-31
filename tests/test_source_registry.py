"""#96 source registry gate / assurance A28: a forbidden or unknown source is never fetched, every decision is ledgered, rate limits hold."""
from datetime import timedelta

import pytest

from jobs.sources import SourceGate, SourceRefused


@pytest.fixture
def gate(conn, clock):
    from core.audit.ledger import Ledger
    return SourceGate(conn, clock, Ledger(conn, clock))


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return {"ok": True}


@pytest.mark.parametrize("source_id", ["zomato", "swiggy", "indiamart", "justdial"])
def test_manual_only_sources_are_never_fetched(gate, source_id):
    spy = Spy()
    with pytest.raises(SourceRefused) as e:
        gate.fetch(source_id, spy, "https://example.invalid/page")
    assert "manual_only" in e.value.reason and spy.calls == []


def test_a_source_that_is_not_in_the_registry_is_refused_by_default(gate):
    spy = Spy()
    with pytest.raises(SourceRefused) as e:
        gate.fetch("shady_directory", spy)
    assert "default deny" in e.value.reason and spy.calls == []


@pytest.mark.parametrize("source_id", ["gst_api", "places_api", "udyam", "fssai", "mca", "ondc", "company_websites"])
def test_registered_allowed_sources_fetch(gate, source_id):
    spy = Spy()
    assert gate.fetch(source_id, spy, "q") == {"ok": True} and len(spy.calls) == 1


def test_a_permitted_scrape_needs_robots_ok_and_a_current_review(gate, conn, clock):
    spy = Spy()
    conn.execute("UPDATE sources SET robots_ok = 0 WHERE source_id = 'company_websites'")
    with pytest.raises(SourceRefused) as e:
        gate.fetch("company_websites", spy)
    assert "robots" in e.value.reason
    conn.execute("UPDATE sources SET robots_ok = 1 WHERE source_id = 'company_websites'")
    clock.advance(timedelta(days=200))                              # the registry entry (reviewed 30 days before the sim start) is now stale
    with pytest.raises(SourceRefused) as e:
        gate.fetch("company_websites", spy)
    assert "overdue for review" in e.value.reason and spy.calls == []


def test_the_rate_limit_is_per_minute_on_the_sim_clock(gate, conn, clock):
    conn.execute("UPDATE sources SET rate_limit_per_min = 3 WHERE source_id = 'udyam'")
    spy = Spy()
    for _ in range(3):
        gate.fetch("udyam", spy)
    with pytest.raises(SourceRefused) as e:
        gate.fetch("udyam", spy)
    assert "rate limit" in e.value.reason and len(spy.calls) == 3
    clock.advance(timedelta(minutes=2))
    gate.fetch("udyam", spy)
    assert len(spy.calls) == 4


def test_every_decision_is_ledgered_with_the_rule_id_on_refusals(gate, conn):
    import json
    gate.fetch("gst_api", Spy())
    with pytest.raises(SourceRefused):
        gate.fetch("zomato", Spy())
    rows = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM ledger WHERE kind = 'POLICY' ORDER BY seq")]
    assert [(r["source_id"], r["allow"], r["rule_ids"]) for r in rows] == [("gst_api", True, []), ("zomato", False, ["SOURCE-REGISTRY"])]


def test_the_seeded_registry_has_no_unlisted_access_class(conn):
    assert {r[0] for r in conn.execute("SELECT DISTINCT access FROM sources")} <= {"official_api", "licensed_api", "permitted_scrape", "manual_only"}
