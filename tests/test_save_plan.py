"""The one save attempt: fitted to the person, offers inside the band need no one, bigger ones need a sign-off, and a merchant is never saved twice."""
import pytest

from core.memory.profile import ProfileStore
from jobs.save_plan import (APPROVED, BIG_MONTHLY_PAISE, DONE, LET_GO, NONE_NEEDED, OFFER, PROPOSED, REP_MONTHLY_PAISE, REP_VISIT, SALES_LEAD, SAVED, AlreadyAttempted, NotAllowed,
                            NotEligible, close_attempt, execute_offer, get_attempt, may_try_to_save, plan_save, sign_off, start_attempt)
from tests.checkpoint_script import SCRIPT
from tests.world import World

MID = "M-0001"
DETAIL = {"ratios": [0.41, 0.4, 0.39], "area_median_ratio": 0.97}


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


def band(w, mid=MID):
    return w.conn.execute("SELECT b.max_months FROM offer_bands b JOIN merchants m ON m.segment = b.segment WHERE m.merchant_id = ?", (mid,)).fetchone()[0]


def plan(w, monthly, diagnosis="COMPETITOR_SWITCH", mid=MID):
    return plan_save(w.conn, mid, diagnosis, DETAIL, monthly)


def test_a_service_or_area_dip_is_not_a_save(world):
    for d in ("DEVICE_OFFLINE", "SETTLEMENT_FAILURE", "AREA_WIDE_DIP", "AREA_OUTAGE"):
        p = plan(world, 10_000_000, d)
        assert p.eligible is False and "not a reason to run a save" in p.refusal and p.approach is None


def test_a_small_merchant_gets_an_offer_inside_the_band_and_needs_no_sign_off(world):
    p = plan(world, REP_MONTHLY_PAISE - 1)
    assert (p.approach, p.offer_months, p.band_months, p.signoff) == (OFFER, band(world), band(world), NONE_NEEDED)


def test_a_merchant_worth_a_reps_day_gets_a_rep_with_a_briefing_still_inside_the_band(world):
    p = plan(world, REP_MONTHLY_PAISE)
    assert p.approach == REP_VISIT and p.offer_months == band(world) and p.signoff == NONE_NEEDED and p.briefing


def test_a_very_large_merchant_may_be_offered_one_more_month_but_only_with_a_sales_leads_sign_off(world):
    p = plan(world, BIG_MONTHLY_PAISE)
    assert p.offer_months == band(world) + 1 and p.signoff == SALES_LEAD and any("sign" in r for r in p.reasons)


def test_the_briefing_has_a_source_for_every_point_and_tells_the_rep_it_is_one_attempt(world):
    pts = plan(world, REP_MONTHLY_PAISE).briefing
    assert all(p["source"] for p in pts) and any(p["source"] == "health:ratios" and "0.41, 0.4, 0.39" in p["text"] for p in pts)
    assert any(p["source"] == "policy:one_attempt" for p in pts) and any(p["source"] == "policy:offer_band" for p in pts)


def test_a_confirmed_rival_sighting_appears_in_the_briefing(world):
    from jobs.competitor_sightings import SightingBook
    rep = world.conn.execute("SELECT rep_id FROM reps LIMIT 1").fetchone()[0]
    s = SightingBook(world.conn, world.clock).record(rep, MID, "photo-9", {"qr_visible": True, "network": "rival_a", "confidence": 0.9})
    assert any(p["source"] == f"sighting:{s.sighting_id}" for p in plan(world, REP_MONTHLY_PAISE).briefing)


def test_there_is_exactly_one_attempt_per_merchant(world):
    p = plan(world, 1_000_000)
    first = start_attempt(world.conn, world.clock, world.ledger, p, "COMPETITOR_SWITCH")
    assert first["status"] == APPROVED and first["merchant_id"] == MID
    with pytest.raises(AlreadyAttempted):
        start_attempt(world.conn, world.clock, world.ledger, p, "COMPETITOR_SWITCH")
    again = plan(world, 1_000_000)
    assert again.eligible is False and "only one attempt" in again.refusal and may_try_to_save(world.conn, MID) is False


def test_a_plan_that_is_not_a_save_cannot_be_started(world):
    with pytest.raises(NotEligible):
        start_attempt(world.conn, world.clock, world.ledger, plan(world, 1_000_000, "DEVICE_OFFLINE"), "DEVICE_OFFLINE")


def test_a_bigger_offer_waits_until_a_sales_lead_or_ops_admin_signs(world):
    a = start_attempt(world.conn, world.clock, world.ledger, plan(world, BIG_MONTHLY_PAISE), "COMPETITOR_SWITCH")
    assert a["status"] == PROPOSED and a["signed_by"] is None
    with pytest.raises(ValueError):
        execute_offer(world.executor, world.conn, MID)                                   # not approved yet: nothing is applied
    with pytest.raises(NotAllowed):
        sign_off(world.conn, world.clock, world.ledger, MID, "support_executive", "x")
    done = sign_off(world.conn, world.clock, world.ledger, MID, "sales_lead", "asha")
    assert done["status"] == APPROVED and done["signed_by"] == "sales_lead:asha"
    with pytest.raises(ValueError):
        sign_off(world.conn, world.clock, world.ledger, MID, "sales_lead", "asha")       # already signed


def test_an_approved_offer_goes_through_the_executor_and_policy_has_the_last_word(world):
    start_attempt(world.conn, world.clock, world.ledger, plan(world, 1_000_000), "COMPETITOR_SWITCH")
    out = execute_offer(world.executor, world.conn, MID)
    assert out.status in ("OK", "REFUSED", "ESCALATE")                                    # a policy refusal is an answer, never an exception
    if out.status == "OK":
        assert world.conn.execute("SELECT COUNT(*) FROM offers WHERE merchant_id = ?", (MID,)).fetchone()[0] == 1
        assert execute_offer(world.executor, world.conn, MID).status == "REPLAYED"


def test_an_offer_with_no_offer_in_it_is_refused(world):
    world.conn.execute("DELETE FROM offer_bands")
    start_attempt(world.conn, world.clock, world.ledger, plan(world, REP_MONTHLY_PAISE), "COMPETITOR_SWITCH")
    with pytest.raises(ValueError):
        execute_offer(world.executor, world.conn, MID)


def test_letting_them_go_is_final_and_leaves_a_fact_saying_so(world):
    start_attempt(world.conn, world.clock, world.ledger, plan(world, 1_000_000), "COMPETITOR_SWITCH")
    a = close_attempt(world.conn, world.clock, world.ledger, MID, LET_GO)
    assert a["status"] == DONE and a["outcome"] == LET_GO and a["closed_at"]
    assert ProfileStore(world.conn, world.clock).get(MID, "save_closed").value == "let_go"
    assert plan(world, 1_000_000).eligible is False and "let go" in plan(world, 1_000_000).refusal
    with pytest.raises(ValueError):
        close_attempt(world.conn, world.clock, world.ledger, MID, SAVED)


def test_saving_closes_the_attempt_without_a_let_go_fact(world):
    start_attempt(world.conn, world.clock, world.ledger, plan(world, 1_000_000), "COMPETITOR_SWITCH")
    assert close_attempt(world.conn, world.clock, world.ledger, MID, SAVED)["outcome"] == SAVED
    assert ProfileStore(world.conn, world.clock).get(MID, "save_closed") is None


def test_a_bad_outcome_or_an_unknown_merchant_is_refused(world):
    with pytest.raises(ValueError):
        close_attempt(world.conn, world.clock, world.ledger, MID, "MAYBE")
    with pytest.raises(KeyError):
        close_attempt(world.conn, world.clock, world.ledger, MID, SAVED)
    assert get_attempt(world.conn, MID) is None


def test_the_database_itself_refuses_a_second_attempt(world):
    import sqlite3
    start_attempt(world.conn, world.clock, world.ledger, plan(world, 1_000_000), "COMPETITOR_SWITCH")
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("INSERT INTO save_attempts(attempt_id, merchant_id, approach, offer_months, signoff, status, diagnosis, created_at) VALUES ('SV-X', ?, 'offer', 1, 'NONE_NEEDED', 'APPROVED', 'X', 'now')", (MID,))
