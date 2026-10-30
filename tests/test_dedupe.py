"""#98 duplicate matching: seeded duplicates and existing merchants -> 0 reach outreach; doubt blocks; unmatched leads cannot be pitched."""
import json

import pytest

from core.audit.ledger import Ledger
from jobs.dedupe import DedupeJob, compare, name_sim, norm_name, norm_phone, outreach_ready


def seeded_duplicates(conn) -> dict:
    return json.loads(conn.execute("SELECT value FROM meta WHERE key = 'seed_duplicates'").fetchone()[0])


def test_normalisation():
    assert norm_name("STAR KIRANA STORE") == norm_name("Star Kirana Store Pvt") == "star kirana"
    assert norm_phone("+91 90000-00138") == "9000000138" and norm_phone("12345") == ""
    assert name_sim("Sri Krishna Tiffin Centre", "Krishna Tiffin Centre") >= 0.7 and name_sim("Star Kirana", "Moon Chemist") < 0.4


def test_all_forty_seeded_duplicates_are_found_and_none_reaches_outreach(conn, clock):
    dupes = seeded_duplicates(conn)
    res = DedupeJob(conn, clock, Ledger(conn, clock)).run()
    assert set(res.existing) >= set(dupes) and len(dupes) == 40
    for lid, mid in dupes.items():
        row = conn.execute("SELECT state, matched_merchant_id FROM leads WHERE lead_id = ?", (lid,)).fetchone()
        assert (row["state"], row["matched_merchant_id"]) == ("EXISTING_MERCHANT", mid), lid
    ready = {r["lead_id"] for r in outreach_ready(conn)}
    assert not ready & set(dupes)                                   # 0 existing merchants reach new-merchant outreach
    assert not {r["lead_id"] for r in outreach_ready(conn)} & set(res.review + res.duplicates + res.existing)


def test_the_invariant_no_ready_lead_matches_any_merchant_and_every_held_lead_is_justified(conn, clock):
    """Checked by brute force against EVERY merchant, not by counts: a ready lead must not MATCH or even REVIEW-match any merchant, and
    every EXISTING_MERCHANT lead must really MATCH the merchant it points at."""
    DedupeJob(conn, clock).run()
    merchants = {r["merchant_id"]: {"name": r["name"], "phone": r["phone"], "pincode": r["pincode"], "lat": r["lat"], "lng": r["lng"]}
                 for r in conn.execute("SELECT * FROM merchants")}

    def as_lead(r):
        return {"name": r["name"], "phone": r["phone_public"], "pincode": r["pincode"], "lat": r["lat"], "lng": r["lng"]}

    ready = outreach_ready(conn)
    assert len(ready) >= 200
    for r in ready:
        assert json.loads(r["provenance_json"])["dedupe"]["result"] == "CLEAR"
        assert all(compare(as_lead(r), m)[0] is None for m in merchants.values()), r["lead_id"]
    for r in conn.execute("SELECT * FROM leads WHERE state = 'EXISTING_MERCHANT'"):
        assert compare(as_lead(r), merchants[r["matched_merchant_id"]])[0] == "MATCH", r["lead_id"]
    assert {r["state"] for r in conn.execute("SELECT state FROM leads")} <= {"EXISTING_MERCHANT", "DUPLICATE", "NEEDS_REVIEW", "NEW"}


def test_a_lead_that_was_never_matched_cannot_be_pitched(conn):
    assert outreach_ready(conn) == []                               # 300 NEW leads, none checked: nothing is ready


def test_running_twice_changes_nothing(conn, clock):
    DedupeJob(conn, clock).run()
    snapshot = [tuple(r) for r in conn.execute("SELECT lead_id, state, matched_merchant_id, provenance_json FROM leads ORDER BY lead_id")]
    again = DedupeJob(conn, clock).run()
    assert again.existing == again.clear == again.review == again.duplicates == []
    assert snapshot == [tuple(r) for r in conn.execute("SELECT lead_id, state, matched_merchant_id, provenance_json FROM leads ORDER BY lead_id")]


@pytest.mark.parametrize("a,b,expect", [
    ({"name": "Star Kirana Store", "phone": "9000000138", "pincode": "411038", "lat": 18.54, "lng": 73.83},
     {"name": "STAR KIRANA STORE PVT", "phone": "+919000000138", "pincode": "411038", "lat": 18.54, "lng": 73.83}, "MATCH"),
    ({"name": "Star Kirana", "phone": "9000000138", "pincode": "411038", "lat": 18.54, "lng": 73.83},
     {"name": "Moon Chemist", "phone": "9000000138", "pincode": "560001", "lat": 12.9, "lng": 77.6}, "REVIEW"),         # a shared number alone is doubt, not a match
    ({"name": "Star Kirana", "phone": "", "pincode": "411038", "lat": 18.54, "lng": 73.83},
     {"name": "Moon Chemist", "phone": "", "pincode": "411038", "lat": 18.5401, "lng": 73.8301}, None),                  # next door, different business
    ({"name": "Sunrise Threads", "phone": "", "pincode": "411038", "lat": 18.54, "lng": 73.83},
     {"name": "Sunrise Threads", "phone": "", "pincode": "411038", "lat": 18.62, "lng": 73.90}, None),                    # same name, 11 km apart: another outlet
    ({"name": "Sri Lakshmi Bakery", "phone": "", "pincode": "560034", "lat": None, "lng": None},
     {"name": "Lakshmi Bakery", "phone": "", "pincode": "560034", "lat": None, "lng": None}, "MATCH"),                    # no coordinates: name + pincode
])
def test_compare_table(a, b, expect):
    assert compare(a, b)[0] == expect


def test_a_possible_match_is_held_not_pitched(conn, clock):
    m = conn.execute("SELECT * FROM merchants WHERE merchant_id = 'M-0050'").fetchone()
    conn.execute("INSERT INTO leads(lead_id, name, pincode, lat, lng, phone_public, state, confidence, provenance_json) VALUES ('L-9001', 'Unrelated Name Traders', ?, 0, 0, ?, 'NEW', 0.9, '{}')",
                 (m["pincode"] + "x", m["phone"]))                                  # same public phone, nothing else alike
    DedupeJob(conn, clock).run()
    row = conn.execute("SELECT state, matched_merchant_id FROM leads WHERE lead_id = 'L-9001'").fetchone()
    assert row["state"] == "NEEDS_REVIEW" and row["matched_merchant_id"] is None
    assert "L-9001" not in {r["lead_id"] for r in outreach_ready(conn)}


def test_the_same_business_from_two_sources_is_kept_once(conn, clock):
    conn.execute("DELETE FROM leads")
    for lid, src, conf in (("L-8001", "gst_api", 0.9), ("L-8002", "places_api", 0.6)):
        conn.execute("INSERT INTO leads(lead_id, name, pincode, lat, lng, phone_public, source_id, state, confidence, provenance_json) VALUES (?,?,?,?,?,?,?, 'NEW', ?, '{}')",
                     (lid, "Ganesh Sweets" if lid == "L-8001" else "GANESH SWEETS PVT", "560076", 12.9, 77.6, "08012345678", src, conf))
    res = DedupeJob(conn, clock).run()
    assert res.clear == ["L-8001"] and res.duplicates == ["L-8002"]
    assert json.loads(conn.execute("SELECT provenance_json FROM leads WHERE lead_id = 'L-8002'").fetchone()[0])["dedupe"]["duplicate_of"] == "L-8001"
    assert [r["lead_id"] for r in outreach_ready(conn)] == ["L-8001"]
