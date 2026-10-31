"""Sales API: the dip, the one ranked list, the save, sightings, style and reps. Reading changes nothing."""
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
    w = World(db_path, clock, SCRIPT)
    DedupeJob(w.conn, w.clock).run()
    score_ready_leads(w.conn, w.clock)
    return w


@pytest.fixture
def api(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http))


def rep_id(w):
    return w.conn.execute("SELECT rep_id FROM reps ORDER BY rep_id LIMIT 1").fetchone()[0]


def test_the_dip_view_says_what_it_checked_and_the_rules_it_used(api):
    d = api.get("/api/sales/dips").json()
    assert d["checked"] > 0 and {"persist_days", "drop_ratio", "zero_days", "actions"} <= set(d["rules"])
    for f in d["findings"]:
        assert {"merchant_id", "diagnosis", "what", "why", "detail", "quiet", "retention"} <= set(f) and f["quiet"] == (f["action"] == "NO_ACTION")


def test_a_whole_area_dip_is_marked_quiet_and_never_retention(api):
    for f in api.get("/api/sales/dips").json()["findings"]:
        if f["diagnosis"] in ("AREA_WIDE_DIP", "AREA_OUTAGE"):
            assert f["quiet"] is True and f["retention"] is False


def test_the_ranked_work_is_one_list_best_first_with_the_assumptions(api):
    w = api.get("/api/sales/work").json()
    values = [i["value"] for i in w["items"]]
    assert values == sorted(values, reverse=True) and w["counts"]["new_lead"] == len([i for i in w["items"] if i["kind"] == "new_lead"]) > 0
    assert {"p_lose", "p_save", "cost_paise", "days_per_month"} <= set(w["assumptions"])


def test_reading_the_dips_and_the_work_changes_nothing(world, api):
    before = world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    api.get("/api/sales/dips")
    api.get("/api/sales/work")
    assert (world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == before


def test_a_merchant_with_no_lasting_dip_has_no_save_and_cannot_start_one(world, api):
    v = api.get("/api/sales/save/M-0001").json()
    if v["diagnosis"] is None:
        assert v["plan"]["eligible"] is False and v["attempt"] is None
        r = api.post("/api/sales/save/M-0001/attempt")
        assert r.status_code == 409 and r.json()["detail"]["error"] == "NOT_AT_RISK"


def test_a_save_for_an_unknown_merchant_is_404(api):
    assert api.get("/api/sales/save/M-9999").status_code == 404
    assert api.post("/api/sales/save/M-0001/close", json={"outcome": "MAYBE"}).status_code == 422
    assert api.post("/api/sales/save/M-0001/close", json={"outcome": "SAVED"}).status_code == 404


def test_signing_off_with_the_wrong_role_is_403_and_without_an_attempt_is_404(api):
    assert api.post("/api/sales/save/M-0001/sign-off", json={"role": "support_executive", "who": "x"}).status_code in (403, 404)
    assert api.post("/api/sales/save/M-0001/sign-off", json={"role": "sales_lead", "who": "asha"}).status_code == 404


# ---------------------------------------------------------------------------------------------------- sightings
def sighting(world, **kw):
    return {"rep_id": rep_id(world), "merchant_id": "M-0003", "photo_ref": "photo-1", "qr_visible": True, "network": "rival_a", "confidence": 0.9, **kw}


def test_a_confident_sighting_is_confirmed_and_listed_with_the_known_rivals(world, api):
    r = api.post("/api/sales/sightings", json=sighting(world))
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    d = api.get("/api/sales/sightings").json()
    assert [s["sighting_id"] for s in d["sightings"]] == [r.json()["sighting_id"]] and "rival_a" in d["rivals"] and d["min_confidence"] == 0.7


def test_an_unsure_sighting_waits_for_a_person_who_must_hold_the_right_role(world, api):
    sid = api.post("/api/sales/sightings", json=sighting(world, confidence=0.4)).json()["sighting_id"]
    assert api.post(f"/api/sales/sightings/{sid}/review", json={"role": "support_executive", "who": "x", "accept": True}).status_code == 403
    done = api.post(f"/api/sales/sightings/{sid}/review", json={"role": "sales_lead", "who": "asha", "accept": True})
    assert done.status_code == 200 and done.json()["status"] == "CONFIRMED"
    assert api.post(f"/api/sales/sightings/{sid}/review", json={"role": "sales_lead", "who": "asha", "accept": True}).status_code == 409


@pytest.mark.parametrize("body,code", [({"merchant_id": "M-9999"}, 404), ({"rep_id": "R-NOPE"}, 404), ({"confidence": 1.5}, 422), ({"photo_ref": ""}, 422)])
def test_a_bad_sighting_is_refused(world, api, body, code):
    assert api.post("/api/sales/sightings", json=sighting(world, **body)).status_code == code


def test_reviewing_an_unknown_sighting_is_404(api):
    assert api.post("/api/sales/sightings/SG-9999/review", json={"role": "sales_lead", "who": "a", "accept": True}).status_code == 404


# ---------------------------------------------------------------------------------------------------- style and reps
def test_a_lead_gets_its_approach_and_its_first_contact_style(world, api):
    lead = api.get("/api/sales/work").json()["items"]
    lid = next(i["item_id"] for i in lead if i["kind"] == "new_lead")
    d = api.get(f"/api/sales/style/{lid}").json()
    assert d["lead_id"] == lid and d["approach"]["level"] and d["approach"]["reasons"] and d["style"]["kind"] and d["voice_budget_words"] == 45
    assert json.loads(json.dumps(d)) == d and "audio" not in json.dumps(d["style"])


def test_an_existing_merchant_has_no_style_and_an_unknown_lead_is_404(world, api):
    existing = world.conn.execute("SELECT lead_id FROM leads WHERE matched_merchant_id IS NOT NULL LIMIT 1").fetchone()[0]
    r = api.get(f"/api/sales/style/{existing}")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "BRIEF_REFUSED"
    assert api.get("/api/sales/style/L-9999").status_code == 404


def test_reps_come_with_their_languages_win_rates_and_an_incentive_statement(api):
    d = api.get("/api/sales/reps").json()
    assert d["reps"] and set(d["briefing"]) == {"new_months", "senior_months"}
    r = d["reps"][0]
    assert {"rep_id", "name", "languages", "experience_months", "win_rates", "statement"} <= set(r)
    assert r["statement"]["earned_paise"] == 0 and r["statement"]["lines"] == [] and r["statement"]["assumptions"]["pool_paise"] > 0


# ------------------------------------------------------------------------------------------------ the desk: working one lead
def a_new_lead(w):
    return w.conn.execute("SELECT lead_id FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND score IS NOT NULL ORDER BY score DESC LIMIT 1").fetchone()[0]


def test_the_desk_says_where_a_lead_stands_and_what_may_come_next(api, world):
    lid = a_new_lead(world)
    d = api.get(f"/api/sales/leads/{lid}/desk").json()
    assert d["lead_id"] == lid and d["state"] == "NEW" and d["next_states"] == sorted(d["next_states"]) and "RESEARCHED" in d["next_states"]
    assert d["history"] == [] and d["notes"] == [] and isinstance(d["do_not_contact"], bool)


def test_an_unknown_lead_is_a_404_on_every_desk_route(api):
    assert api.get("/api/sales/leads/L-NOPE/desk").status_code == 404
    assert api.post("/api/sales/leads/L-NOPE/contact").status_code == 404
    assert api.post("/api/sales/leads/L-NOPE/move", json={"to": "RESEARCHED"}).status_code == 404
    assert api.post("/api/sales/leads/L-NOPE/note", json={"text": "hi"}).status_code == 404


def test_an_illegal_move_is_refused_with_the_reason_and_changes_nothing(api, world):
    lid = a_new_lead(world)
    r = api.post(f"/api/sales/leads/{lid}/move", json={"to": "WON"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "CANNOT_MOVE"
    assert api.get(f"/api/sales/leads/{lid}/desk").json()["state"] == "NEW"


def test_a_legal_move_is_recorded_and_shows_in_the_history(api, world):
    lid = a_new_lead(world)
    r = api.post(f"/api/sales/leads/{lid}/move", json={"to": "RESEARCHED", "reason": "checked by hand"})
    assert r.status_code == 200 and r.json()["state"] == "RESEARCHED"
    h = api.get(f"/api/sales/leads/{lid}/desk").json()["history"]
    assert [(x["from_state"], x["to_state"], x["reason"]) for x in h] == [("NEW", "RESEARCHED", "checked by hand")]


def test_a_note_is_stored_and_a_secret_in_it_is_redacted_first(api, world):
    lid = a_new_lead(world)
    r = api.post(f"/api/sales/leads/{lid}/note", json={"rep_id": rep_id(world), "text": "call back, the OTP is 482913"})
    assert r.status_code == 200 and "482913" not in r.json()["text"]
    notes = api.get(f"/api/sales/leads/{lid}/desk").json()["notes"]
    assert len(notes) == 1 and "482913" not in notes[0]["text"] and notes[0]["rep_id"] == rep_id(world)


def test_an_empty_note_is_rejected(api, world):
    assert api.post(f"/api/sales/leads/{a_new_lead(world)}/note", json={"text": ""}).status_code == 422


def test_a_first_contact_always_answers_with_a_status_and_the_reasons_when_refused(api, world):
    r = api.post(f"/api/sales/leads/{a_new_lead(world)}/contact")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("SENT", "ROUTED_TO_FIELD", "BLOCKED", "NO_CONSENTED_CHANNEL", "NOT_ELIGIBLE", "SEND_FAILED")
    if body["status"] not in ("SENT", "ROUTED_TO_FIELD"):
        assert body["reasons"]
