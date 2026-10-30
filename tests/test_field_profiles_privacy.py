"""#105 rep and technician profiles, #113 field privacy and coaching."""
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from core.audit.ledger import Ledger
from field.privacy import ANALYSIS_CHANNEL, ConsentRequired, FieldPrivacy, LocationRejected
from field.profiles import (NotAllowed, ProfileEditor, assert_no_protected_fields, load_rep, load_reps, load_tech, load_techs, minutes, shrunk_win_rate,
                            team_prior)

ROOT = Path(__file__).resolve().parent.parent


# ================================================================== #105
def test_seeded_profiles_load_with_levels_shift_training_and_stock(conn):
    reps, techs = load_reps(conn), load_techs(conn)
    assert [r.rep_id for r in reps] == ["R-01", "R-02", "R-03", "R-04", "R-05"] and len(techs) == 8
    suresh = load_rep(conn, "R-01")
    assert suresh.shift == (600, 1140) and suresh.speaks("kn-IN", "native") and suresh.speaks("hi-IN") and not suresh.speaks("en-IN")      # basic English only
    assert suresh.trained_for("loans") and suresh.win_rates["restaurant"] == 0.32 and suresh.base == (12.99, 77.55)
    anita = load_rep(conn, "R-04")
    assert anita.experience_months == 8 and not anita.trained_for("loans") and anita.trained_for("soundbox")                                 # 8 months: not loan-trained
    t = load_tech(conn, "T-07")
    assert t.skills == {"installation", "network", "soundbox_repair"} and t.speaks("kn-IN", "fluent") and t.max_jobs_per_day == 8 and t.shift == (540, 1080)
    assert minutes("09:30") == 570


def test_a_missing_person_is_an_error_not_a_default(conn):
    with pytest.raises(KeyError):
        load_rep(conn, "R-99")
    with pytest.raises(KeyError):
        load_tech(conn, "T-99")


def test_no_protected_trait_is_stored_and_a_sneaked_in_column_is_caught(conn):
    assert_no_protected_fields(conn)
    conn.execute("ALTER TABLE reps ADD COLUMN community TEXT")
    with pytest.raises(AssertionError) as e:
        assert_no_protected_fields(conn)
    assert "community" in str(e.value)


def test_shrinkage_pulls_thin_evidence_toward_the_prior():
    assert shrunk_win_rate(0.5, 0, 0.2) == 0.2                     # no visits: the prior
    assert shrunk_win_rate(0.5, 10, 0.2, 10) == pytest.approx(0.35)
    assert shrunk_win_rate(0.5, 1000, 0.2, 10) == pytest.approx(0.497, abs=0.001)
    assert shrunk_win_rate(0.5, 3, 0.2) < shrunk_win_rate(0.5, 30, 0.2) < 0.5
    with pytest.raises(ValueError):
        shrunk_win_rate(0.5, -1, 0.2)


def test_team_prior_is_the_mean_rate_of_the_reps_who_have_one(conn):
    reps = load_reps(conn)
    assert team_prior(reps, "restaurant") == pytest.approx((0.32 + 0.30 + 0.22) / 3)
    assert team_prior(reps, "no_such_segment", default=0.1) == 0.1


def test_a_person_sees_and_edits_their_own_profile_and_it_is_ledgered(conn, clock):
    ed = ProfileEditor(conn, Ledger(conn, clock))
    assert ed.view("R-02", "R-02").rep_id == "R-02"
    out = ed.update("R-02", "R-02", {"languages": {"kn-IN": "native", "en-IN": "fluent", "hi-IN": "basic"}, "shift_start": "09:30"})
    assert out["changed"] == ["languages", "shift_start"]
    r = load_rep(conn, "R-02")
    assert r.speaks("hi-IN", "basic") and r.shift[0] == 570
    e = [x for x in Ledger(conn, clock).entries() if x.kind == "HUMAN_ACTION"][-1]
    assert e.actor == "HUMAN:R-02" and e.payload == {"action": "profile_edit", "person": "R-02", "fields": ["languages", "shift_start"]}


def test_nobody_else_may_view_or_edit_but_a_manager_may(conn):
    ed = ProfileEditor(conn)
    with pytest.raises(NotAllowed):
        ed.view("R-02", "R-03")
    with pytest.raises(NotAllowed):
        ed.update("R-02", "R-03", {"shift_start": "08:00"})
    assert ed.view("R-02", "boss-1", "area_manager").rep_id == "R-02"
    ed.update("T-01", "boss-1", {"parts": {"soundbox_unit": 9}}, "ops_admin")
    assert load_tech(conn, "T-01").parts == {"soundbox_unit": 9}


def test_system_derived_fields_and_protected_traits_cannot_be_edited_even_by_the_owner(conn):
    ed = ProfileEditor(conn)
    for bad in ({"win_rates": {"kirana": 0.99}}, {"experience_months": 99}, {"community": "x"}, {"religion": "x"}, {"rep_id": "R-77"}):
        with pytest.raises(NotAllowed):
            ed.update("R-01", "R-01", bad)
    assert load_rep(conn, "R-01").win_rates["kirana"] == 0.28 and load_rep(conn, "R-01").experience_months == 14


# ================================================================== #113
def route(conn, clock, person="R-01", kind="rep", active=True, day=None):
    day = day or clock.now().date().isoformat()
    conn.execute("INSERT INTO routes VALUES (?,?,?,?,?,?)", (f"RT-{person}-{day}-{active}", person, kind, day, json.dumps({"active": active, "stops": []}), clock.now_iso()))


def at(clock, hhmm):
    h, m = hhmm.split(":")
    clock.set(clock.now().replace(hour=int(h), minute=int(m), second=0))


def test_location_is_recorded_only_inside_the_shift_with_an_active_route(conn, clock):
    priv = FieldPrivacy(conn, clock)
    route(conn, clock)
    at(clock, "11:00")
    assert priv.record_location("R-01", "rep", 12.99, 77.55) > 0
    assert conn.execute("SELECT COUNT(*) FROM field_locations").fetchone()[0] == 1


@pytest.mark.parametrize("hhmm", ["06:00", "09:59", "19:00", "22:30"])
def test_location_outside_the_shift_is_rejected_and_nothing_is_stored(conn, clock, hhmm):
    priv = FieldPrivacy(conn, clock)
    route(conn, clock)
    at(clock, hhmm)
    with pytest.raises(LocationRejected) as e:
        priv.record_location("R-01", "rep", 12.99, 77.55)
    assert e.value.reason == "OUTSIDE_SHIFT" and conn.execute("SELECT COUNT(*) FROM field_locations").fetchone()[0] == 0


def test_no_active_route_means_no_tracking_even_inside_the_shift(conn, clock):
    priv = FieldPrivacy(conn, clock)
    at(clock, "11:00")
    with pytest.raises(LocationRejected) as e:
        priv.record_location("R-01", "rep", 1.0, 2.0)
    assert e.value.reason == "NO_ACTIVE_ROUTE"
    route(conn, clock, active=False)
    with pytest.raises(LocationRejected):
        priv.record_location("R-01", "rep", 1.0, 2.0)
    route(conn, clock, day=(clock.now() + timedelta(days=1)).date().isoformat())             # tomorrow's route is not today's
    with pytest.raises(LocationRejected):
        priv.record_location("R-01", "rep", 1.0, 2.0)


def test_technician_shift_is_their_own_and_rejections_leave_no_coordinates_in_the_ledger(conn, clock):
    ledger = Ledger(conn, clock)
    priv = FieldPrivacy(conn, clock, ledger)
    route(conn, clock, "T-01", "technician")
    at(clock, "09:30")                                   # inside T-01's 09:00-18:00 shift, but before a rep's 10:00 start
    assert priv.record_location("T-01", "technician", 12.93, 77.62) > 0
    at(clock, "18:30")
    with pytest.raises(LocationRejected):
        priv.record_location("T-01", "technician", 12.9312345, 77.6212345)
    assert "12.9312345" not in json.dumps([e.payload for e in ledger.entries()])


def test_only_the_owner_sees_or_deletes_their_location_and_notes(conn, clock):
    priv = FieldPrivacy(conn, clock)
    route(conn, clock)
    at(clock, "11:00")
    lid = priv.record_location("R-01", "rep", 12.99, 77.55)
    assert priv.own_data("R-01", "R-01")["locations"][0]["lat"] == 12.99
    with pytest.raises(NotAllowed):
        priv.own_data("R-01", "R-02")
    with pytest.raises(NotAllowed):
        priv.own_data("R-01", "boss-1")                  # not even a manager gets raw location through this module
    with pytest.raises(NotAllowed):
        priv.delete_location(lid, "R-02")
    assert priv.delete_location(lid, "R-01") is True and priv.own_data("R-01", "R-01")["locations"] == []
    assert priv.delete_location(999, "R-01") is False


def test_analysis_without_consent_is_refused_and_nothing_is_stored(conn, clock):
    priv = FieldPrivacy(conn, clock)
    with pytest.raises(ConsentRequired) as e:
        priv.analyse_visit("R-01", "V-1", {"strengths": ["good opening"]})
    assert e.value.rule_id == "CONSENT-COACH-001" and conn.execute("SELECT COUNT(*) FROM coaching_notes").fetchone()[0] == 0
    conn.execute("INSERT INTO consent VALUES ('R-01', ?, 'REVOKED', 'self', ?)", (ANALYSIS_CHANNEL, clock.now_iso()))
    with pytest.raises(ConsentRequired):
        priv.analyse_visit("R-01", "V-1", {"strengths": []})
    conn.execute("INSERT INTO consent VALUES ('R-02', 'whatsapp', 'GRANTED', 'self', ?)", (clock.now_iso(),))            # a different channel is not analysis consent
    with pytest.raises(ConsentRequired):
        priv.analyse_visit("R-02", "V-1", {"strengths": []})


def test_with_consent_the_output_is_a_coaching_note_with_no_score_rank_or_penalty(conn, clock):
    priv = FieldPrivacy(conn, clock)
    conn.execute("INSERT INTO consent VALUES ('R-01', ?, 'GRANTED', 'self', ?)", (ANALYSIS_CHANNEL, clock.now_iso()))
    note = priv.analyse_visit("R-01", "V-1", {"strengths": ["asked about the shop first"], "suggestions": ["offer the loan after the device issue"]})
    assert set(note) == {"note_id", "person_id", "visit_ref", "created_at", "strengths", "suggestions"} and note["suggestions"] == ["offer the loan after the device issue"]
    for forbidden in ("score", "rank", "rating", "penalty", "grade"):
        assert not any(forbidden in k for k in note)
        with pytest.raises(ValueError):
            priv.analyse_visit("R-01", "V-2", {"strengths": [], forbidden: 3})
    assert priv.own_data("R-01", "R-01")["coaching_notes"][0]["strengths"] == ["asked about the shop first"]


def test_the_analysis_channel_matches_the_policy_limits():
    limits = yaml.safe_load((ROOT / "packs/paytm_merchant/policy/limits.yaml").read_text(encoding="utf-8"))
    assert limits["consent"]["analysis_channel"] == ANALYSIS_CHANNEL


@pytest.mark.parametrize("hhmm,inside", [("22:30", True), ("23:59", True), ("01:00", True), ("05:59", True), ("06:00", False), ("12:00", False), ("21:59", False)])
def test_an_overnight_shift_wraps_midnight(conn, clock, monkeypatch, hhmm, inside):
    priv = FieldPrivacy(conn, clock)
    monkeypatch.setattr(priv, "_shift", lambda person_id, kind: (22 * 60, 6 * 60))     # 22:00 - 06:00
    route(conn, clock)
    at(clock, hhmm)
    if inside:
        assert priv.record_location("R-01", "rep", 12.99, 77.55) > 0
    else:
        with pytest.raises(LocationRejected):
            priv.record_location("R-01", "rep", 12.99, 77.55)
