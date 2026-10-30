"""#97 discovery: a demo run stores only business fields with provenance; no personal data anywhere; forbidden sources are never fetched;
Layer 1 emits upsell targets (existing merchants) and never new leads."""
import json

import pytest

from core.audit.ledger import Ledger
from jobs.dedupe import DedupeJob, outreach_ready
from jobs.discovery import STORED_FIELDS, DiscoveryJob, business_only
from jobs.synthetic_sources import OWNERS, make_fetcher

PINS = ["560034", "560038"]
SOURCES = ["gst_api", "udyam", "fssai", "places_api"]


@pytest.fixture
def job(conn, clock):
    conn.execute("DELETE FROM leads")
    return DiscoveryJob(conn, clock, Ledger(conn, clock))


def run(job, sources=SOURCES):
    return job.discover({s: make_fetcher(s) for s in sources}, PINS)


def test_a_demo_run_stores_only_business_fields_with_provenance(job, conn):
    res = run(job)
    assert len(res.stored) == 4 * 2 * 4 and res.refused == {}
    for r in conn.execute("SELECT * FROM leads"):
        prov = json.loads(r["provenance_json"])
        stored = {k for k in STORED_FIELDS if r[k] not in (None, "")}
        assert set(prov) == stored                                  # every stored field has provenance and nothing else does
        for f, p in prov.items():
            assert set(p) == {"source", "fetched_at", "confidence"} and p["source"] == r["source_id"] and 0 < p["confidence"] <= 1
        assert r["state"] == "NEW"


def test_no_personal_data_is_stored_anywhere_in_the_database(job, conn):
    """Checks the VALUES a page carried (names, mobiles, e-mails, ID numbers), not the field names the ledger counts."""
    res = run(job)
    raw = [r for s in SOURCES for r in make_fetcher(s)(PINS)]
    dump = " ".join(str(v) for t in ("leads", "ledger", "events", "meta") for row in conn.execute(f"SELECT * FROM {t}") for v in tuple(row))
    personal = {r["owner_name"] for r in raw} | {r["owner_mobile"] for r in raw} | {r["email"] for r in raw} | {r["aadhaar"] for r in raw} | {r["pan"] for r in raw}
    personal |= {r["phone"] for r in raw if r["phone_kind"] == "personal"}
    assert len(personal) > 20
    for value in personal:
        assert value not in dump, value
    assert res.dropped_fields["owner_name"] == len(raw) and {"owner_mobile", "email", "aadhaar", "pan", "phone"} <= set(res.dropped_fields)


def test_a_personal_mobile_is_dropped_but_the_business_is_kept(job, conn):
    run(job)
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE phone_public IS NULL").fetchone()[0] == 4 * 2        # the i == 3 record of each pincode and source
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE phone_public LIKE '97%'").fetchone()[0] == 0


def test_business_only_unit():
    f, dropped = business_only({"name": "Sri Provision Store", "pincode": "560034", "phone": "08012345678", "phone_kind": "business", "owner_name": "X", "email": "a@b"})
    assert f == {"name": "Sri Provision Store", "pincode": "560034", "phone_public": "08012345678"} and sorted(dropped) == ["email", "owner_name", "phone_kind"]
    assert business_only({"owner_name": "Only A Person"})[0] == {}                                   # no business name: nothing is stored
    assert "phone_public" not in business_only({"name": "X", "phone": "9876543210"})[0]            # an unlabelled phone is treated as personal


@pytest.mark.parametrize("source_id", ["zomato", "swiggy", "indiamart", "justdial", "made_up_directory"])
def test_a_forbidden_or_unknown_source_is_never_fetched(job, conn, source_id):
    calls = []
    res = job.discover({source_id: lambda pins: calls.append(pins) or []}, PINS)
    assert calls == [] and source_id in res.refused and res.stored == []
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_one_refused_source_does_not_stop_the_others(job):
    res = job.discover({"zomato": make_fetcher("zomato"), "gst_api": make_fetcher("gst_api")}, PINS)
    assert "zomato" in res.refused and len(res.stored) == 8


def test_running_again_stores_nothing_twice(job, conn):
    run(job)
    n = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    again = run(job)
    assert again.stored == [] and again.skipped_existing == n and conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == n


def test_layer_one_emits_upsell_targets_for_existing_merchants_never_new_leads(job, conn):
    conn.execute("DELETE FROM lender_apps")
    res = run(job, sources=[])
    assert res.stored == [] and conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    reasons = {r["reason"] for r in conn.execute("SELECT reason FROM upsell_targets")}
    assert "REGISTERED_NO_DEVICE" in reasons and "ABANDONED_SIGNUP" in reasons
    # registered-no-device merchants are ranked by their own 30-day QR volume, highest first
    ranked = [r["rank_value"] for r in conn.execute("SELECT rank_value FROM upsell_targets WHERE reason = 'REGISTERED_NO_DEVICE' ORDER BY rank_value DESC")]
    assert ranked == sorted(ranked, reverse=True) and conn.execute("SELECT COUNT(*) FROM upsell_targets WHERE reason = 'REGISTERED_NO_DEVICE'").fetchone()[0] == 94
    for r in conn.execute("SELECT merchant_id FROM upsell_targets"):
        assert conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (r["merchant_id"],)).fetchone()
    assert json.loads(conn.execute("SELECT detail_json FROM upsell_targets LIMIT 1").fetchone()[0])["source"] == "paytm_data"


def test_discovered_leads_flow_into_dedupe_and_only_cleared_ones_are_ready(job, conn, clock):
    run(job)
    assert outreach_ready(conn) == []                                    # discovered is not ready
    m = conn.execute("SELECT * FROM merchants WHERE merchant_id = 'M-0003'").fetchone()
    conn.execute("INSERT INTO leads(lead_id, name, pincode, lat, lng, phone_public, source_id, state, confidence, provenance_json) VALUES ('L-7777', ?, ?, ?, ?, ?, 'gst_api', 'NEW', 0.99, '{}')",
                 (m["name"].upper(), m["pincode"], m["lat"], m["lng"], m["phone"]))
    DedupeJob(conn, clock).run()
    ready = {r["lead_id"] for r in outreach_ready(conn)}
    assert "L-7777" not in ready and len(ready) >= 1
    assert conn.execute("SELECT matched_merchant_id FROM leads WHERE lead_id = 'L-7777'").fetchone()[0] == "M-0003"


def test_the_run_is_ledgered_with_counts_and_no_record_contents(job, conn):
    run(job)
    e = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM ledger WHERE kind = 'STATE'")][-1]
    assert e["job"] == "discovery" and e["stored"] == 32 and "owner_name" in e["dropped_fields"]
    assert not any(o in json.dumps(e) for o in OWNERS)
