"""Leads API: the new-merchant funnel for the dashboard. READ-ONLY over the growth jobs' own tables: discovery, duplicate matching and scoring ran before
(here: the same jobs the demo stack runs at start); the API only shows what they decided, and the brief is the brief job's, whose numbers all cite a source."""
import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from jobs.dedupe import DedupeJob
from jobs.scoring import score_ready_leads
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def ran(world):
    """The growth jobs have run on the seed: duplicate matching, then scoring of the cleared leads."""
    DedupeJob(world.conn, world.clock).run()
    score_ready_leads(world.conn, world.clock)
    return world


@pytest.fixture
def api(ran):
    return TestClient(create_api(ran.db_path, ran.clock, ran.executor, ran.http))


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def test_the_funnel_accounts_for_every_lead_found(api, ran):
    f = api.get("/api/leads").json()["funnel"]
    assert f["found"] == one(ran.conn, "SELECT COUNT(*) FROM leads")[0] == 300
    assert (f["existing_merchant"], f["needs_review"], f["duplicate"], f["cleared"]) == (40, 26, 0, 234)       # the seed's hand-planted duplicates
    assert f["existing_merchant"] + f["needs_review"] + f["duplicate"] + f["cleared"] + f["not_checked"] == f["found"]


def test_before_duplicate_matching_nothing_is_cleared_and_nothing_is_ranked(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    out = TestClient(create_api(w.db_path, w.clock, w.executor, w.http)).get("/api/leads").json()
    assert out["funnel"]["cleared"] == 0 and out["funnel"]["not_checked"] == 300 and out["leads"] == []


def test_the_ranked_list_is_only_cleared_leads_best_score_first(api, ran):
    leads = api.get("/api/leads").json()["leads"]
    assert len(leads) == 234
    scores = [l["score"] for l in leads]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]
    ids = {l["lead_id"] for l in leads}
    existing = {r["lead_id"] for r in ran.conn.execute("SELECT lead_id FROM leads WHERE matched_merchant_id IS NOT NULL")}
    assert len(existing) == 40 and not ids & existing                                           # an existing merchant is never offered as a new lead


def test_every_ranked_lead_has_what_the_map_and_the_list_need(api):
    l = api.get("/api/leads").json()["leads"][0]
    assert set(l) >= {"lead_id", "name", "category", "pincode", "lat", "lng", "source_id", "confidence", "score", "channel", "uncertain",
                      "monthly_rupees", "low_rupees", "high_rupees", "do_not_contact"}
    assert l["channel"] in ("field_rep", "ai_voice", "self_serve") and l["low_rupees"] <= l["monthly_rupees"] <= l["high_rupees"]


def test_excluded_leads_say_why_and_name_the_merchant_they_match(api, ran):
    out = api.get("/api/leads").json()
    ex = {e["lead_id"]: e for e in out["excluded"]}
    row = one(ran.conn, "SELECT lead_id, matched_merchant_id FROM leads WHERE matched_merchant_id IS NOT NULL ORDER BY lead_id")
    assert ex[row["lead_id"]]["state"] == "EXISTING_MERCHANT" and ex[row["lead_id"]]["matched_merchant_id"] == row["matched_merchant_id"]
    assert all(e["reason"] for e in ex.values())


def test_the_map_bounds_cover_every_lead_shown(api):
    out = api.get("/api/leads").json()
    b = out["bounds"]
    assert all(b["min_lat"] <= l["lat"] <= b["max_lat"] and b["min_lng"] <= l["lng"] <= b["max_lng"] for l in out["leads"])


def test_a_number_on_do_not_disturb_is_marked(api, ran):
    lead, phone = tuple(one(ran.conn, "SELECT lead_id, phone_public FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL ORDER BY lead_id LIMIT 1"))
    ran.conn.execute("INSERT OR IGNORE INTO dnd_registry(phone) VALUES (?)", (phone,))
    leads = {l["lead_id"]: l for l in api.get("/api/leads").json()["leads"]}
    assert leads[lead]["do_not_contact"] is True
    assert sum(l["do_not_contact"] for l in leads.values()) == one(ran.conn, "SELECT COUNT(*) FROM leads l JOIN dnd_registry d ON d.phone = l.phone_public "
                                                                    "WHERE l.state = 'NEW' AND l.matched_merchant_id IS NULL")[0]


# ---------------------------------------------------------------------------------------------------- the brief
def top(api):
    return api.get("/api/leads").json()["leads"][0]["lead_id"]


def test_a_lead_comes_with_its_brief_and_the_brief_passes_its_own_gate(api):
    d = api.get(f"/api/leads/{top(api)}").json()
    assert d["lead"]["lead_id"] == top(api) and d["brief"]["passes_gate"] is True
    b = d["brief"]
    assert b["who"] and b["pitch"][0]["rank"] == "main" and len(b["objections"]) == 4 and b["gaps"]


def test_every_number_in_the_brief_cites_a_source_and_free_text_has_none(api):
    from core.gate.numbers import extract_numbers
    b = api.get(f"/api/leads/{top(api)}").json()["brief"]
    cited = [*b["who"], *b["pitch"], *b["contact"]]
    assert all(c["source"].startswith(("tool:", "kb:", "policy:")) for c in cited)
    free = [*b["uses_now"], *b["needs"], *b["contact_notes"], *b["gaps"]]
    assert all(not extract_numbers(t, english_one=False) for t in free)


def test_objection_answers_are_quoted_from_their_approved_source(api):
    b = api.get(f"/api/leads/{top(api)}").json()["brief"]
    assert {o["key"] for o in b["objections"]} == {"charges_too_high", "already_using_other", "device_rent", "trust"}
    assert all(o["source"].startswith("kb:") and o["text"] for o in b["objections"])


def test_the_detail_shows_where_each_field_came_from(api):
    d = api.get(f"/api/leads/{top(api)}").json()
    assert d["lead"]["source_id"] and d["provenance"]["fields"] and d["provenance"]["dedupe"]["result"] == "CLEAR"
    assert all({"source", "confidence", "fetched_at"} <= set(v) for v in d["provenance"]["fields"].values())


def test_consent_and_do_not_disturb_state_is_shown_not_assumed(api):
    d = api.get(f"/api/leads/{top(api)}").json()
    assert d["contact"]["consented_channels"] == [] and d["contact"]["do_not_contact"] is False          # the seed holds no consent for any lead
    assert d["contact"]["can_contact_now"] is False and "consent" in d["contact"]["why_not"].lower()


def test_a_consented_channel_makes_a_shop_contactable_and_names_the_channel(api, ran):
    lead = one(ran.conn, "SELECT lead_id FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND category != 'd2c' ORDER BY score DESC LIMIT 1")[0]
    ran.conn.execute("INSERT INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'whatsapp', 'GRANTED', 'test', ?)", (lead, ran.clock.now_iso()))
    c = api.get(f"/api/leads/{lead}").json()["contact"]
    assert c["consented_channels"] == ["whatsapp"] and c["can_contact_now"] is True and c["why_not"] is None


def test_an_online_brand_with_only_a_whatsapp_consent_is_not_contactable(api, ran):
    lead = one(ran.conn, "SELECT lead_id FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND category = 'd2c' ORDER BY score DESC LIMIT 1")[0]
    ran.conn.execute("INSERT INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'whatsapp', 'GRANTED', 'test', ?)", (lead, ran.clock.now_iso()))
    c = api.get(f"/api/leads/{lead}").json()["contact"]
    assert c["can_contact_now"] is False                                                            # an online brand is reached by email, which it has not granted


def test_a_lead_on_do_not_disturb_is_never_contactable_even_with_consent(api, ran):
    lead, phone = tuple(one(ran.conn, "SELECT lead_id, phone_public FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND category != 'd2c' "
                                      "ORDER BY score DESC LIMIT 1"))
    ran.conn.execute("INSERT INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'whatsapp', 'GRANTED', 'test', ?)", (lead, ran.clock.now_iso()))
    ran.conn.execute("INSERT OR IGNORE INTO dnd_registry(phone) VALUES (?)", (phone,))
    c = api.get(f"/api/leads/{lead}").json()["contact"]
    assert c["do_not_contact"] is True and c["can_contact_now"] is False and "do not disturb" in c["why_not"].lower()


def test_an_existing_merchant_gets_no_brief_and_the_reason(api, ran):
    row = one(ran.conn, "SELECT lead_id, matched_merchant_id FROM leads WHERE matched_merchant_id IS NOT NULL ORDER BY lead_id")
    r = api.get(f"/api/leads/{row['lead_id']}")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["error"] == "BRIEF_REFUSED" and body["matched_merchant_id"] == row["matched_merchant_id"] and "brief" not in body


def test_an_unknown_lead_is_a_404(api):
    assert api.get("/api/leads/L-9999").status_code == 404


def test_the_demo_stack_runs_the_growth_jobs_once_and_a_second_call_changes_nothing(world):
    from core.api.dev_server import seed_demo_leads
    seed_demo_leads(world)
    snap = lambda: json.dumps([dict(r) for r in world.conn.execute("SELECT * FROM leads ORDER BY lead_id")], default=str)
    first = snap()
    assert one(world.conn, "SELECT COUNT(*) FROM leads WHERE state = 'EXISTING_MERCHANT'")[0] == 40
    assert one(world.conn, "SELECT COUNT(*) FROM leads WHERE score IS NOT NULL")[0] == 234
    seed_demo_leads(world)
    assert snap() == first


def test_reading_leads_changes_nothing(api, ran):
    before = json.dumps([dict(r) for r in ran.conn.execute("SELECT * FROM leads ORDER BY lead_id")], default=str)
    ledger_before = tuple(one(ran.conn, "SELECT COUNT(*), MAX(seq) FROM ledger"))
    api.get("/api/leads")
    api.get(f"/api/leads/{top(api)}")
    assert json.dumps([dict(r) for r in ran.conn.execute("SELECT * FROM leads ORDER BY lead_id")], default=str) == before
    assert tuple(one(ran.conn, "SELECT COUNT(*), MAX(seq) FROM ledger")) == ledger_before
