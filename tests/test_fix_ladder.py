"""The fix ladder: cost, chance and waiting loss decide the rung; repeat and broken devices skip up by arithmetic; people come from their own rules.

Expected costs below were worked by hand for a kirana (a day's volume of 189,750 paise, open 13 hours): the waiting loss is 189,750 * 5% / 13 = 729 paise an hour."""
from datetime import datetime, timedelta, timezone

import pytest

from field.fix_ladder import (BROKEN_P, COST_PAISE, HOURS, REPEAT_P1, SWAP_HOURS, FixPlan, Situation, apply_booking, build_plan, plan_fix, read_situation,
                              resolve_people)
from field.helpers import choose_helper, ensure_roster, load_helpers
from field.simulated_travel import SimulatedRoadProvider
from field.swap import COURIER_COST_PAISE, choose_swap, ensure_couriers
from tests.checkpoint_script import SCRIPT
from tests.world import World

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 31, 0, 0, tzinfo=IST)
ROAD = SimulatedRoadProvider()


def S(**kw) -> Situation:
    base = dict(merchant_id="M-X", language="hi-IN", device_status="OFFLINE", last_test_ok=False, silent_hours=5, remote_attempts=0, replacements_30d=0, chronic=False,
                daily_volume_paise=189_750, open_hours=13, hours_source="ASSUMED")
    return Situation(**{**base, **kw})


def costs(plan: FixPlan) -> dict:
    return {r.rung: r.expected_if_start_here_paise for r in plan.rungs}


# ---------------------------------------------------------------------------------------------------- the choice
def test_a_device_that_is_online_and_announcing_has_nothing_to_fix():
    p = build_plan(S(device_status="ONLINE", last_test_ok=True))
    assert p.start_rung == 0 and p.rungs == [] and "nothing to fix" in p.reasons[0]


def test_the_waiting_loss_is_a_share_of_the_day_spread_over_the_open_hours():
    assert build_plan(S()).loss_per_hour_paise == 189_750 * 500 // 10_000 // 13 == 729
    assert build_plan(S(daily_volume_paise=2 * 189_750)).loss_per_hour_paise == 2 * 189_750 * 500 // 10_000 // 13
    assert build_plan(S(open_hours=6)).loss_per_hour_paise == 189_750 * 500 // 10_000 // 6


def test_a_plain_first_fault_starts_with_the_cheap_remote_fix():
    p = build_plan(S())
    assert p.start_rung == 1 and not p.swap and "cheapest" in p.reasons[0]
    assert costs(p) == {1: 20229, 2: 40103, 3: 97496}                                   # worked by hand: 97496 = 80000 + 24 * 729; 40103 = 15729 + 0.25 * 97496; 20229 = 2182.25 + 0.45 * 40103


def test_a_device_still_offline_after_a_remote_fix_is_clearly_broken_and_goes_to_a_swap():
    p = build_plan(S(remote_attempts=1))
    assert p.start_rung == 3 and p.swap is True
    assert costs(p) == {1: 53723, 2: 53134, 3: 39374}                                   # 39374 = 35000 + 6 * 729; 53134 = 15729 + 0.95 * 39374; 53723 = 2182.25 + 0.97 * 53134.3
    assert any("clearly broken" in r for r in p.reasons) and any("courier" in r for r in p.reasons)
    assert p.rungs[2].cost_paise == COURIER_COST_PAISE and p.rungs[2].hours == SWAP_HOURS


def test_a_device_silent_for_two_days_is_clearly_broken_without_any_remote_attempt():
    p = build_plan(S(silent_hours=60, remote_attempts=0))
    assert p.start_rung == 3 and p.swap is True and "no heartbeat for 60 hours" in p.reasons[0]


def test_a_device_silent_for_two_days_but_online_is_not_broken():
    assert build_plan(S(device_status="ONLINE", last_test_ok=False, silent_hours=60)).start_rung == 1


def test_a_repeat_fault_skips_the_remote_fix_and_goes_to_a_helper_who_can_ask_why():
    p = build_plan(S(replacements_30d=1))
    assert p.start_rung == 2 and not p.swap
    assert costs(p) == {1: 69453, 2: 69352, 3: 97496}                                   # 69352 = 15729 + 0.55 * 97496; 69453 = 2182.25 + 0.97 * 69351.8: a hair above rung 2
    assert p.rungs[0].p_success == REPEAT_P1 and "repeat fault" in p.reasons[0]


def test_a_chronic_flag_counts_as_a_repeat_even_with_no_recent_replacement():
    assert build_plan(S(chronic=True)).start_rung == 2


def test_a_repeat_and_broken_device_needs_a_technician_not_a_courier():
    p = build_plan(S(remote_attempts=1, replacements_30d=1))
    assert p.start_rung == 3 and p.swap is False
    assert p.rungs[2].cost_paise == COST_PAISE[3] and p.rungs[2].hours == HOURS[3]
    assert any("not treated as a simple swap" in r for r in p.reasons)
    assert p.rungs[0].p_success == min(BROKEN_P[0], REPEAT_P1)


def test_the_start_rung_is_always_the_cheapest_expected_cost_and_ties_go_to_the_lower_rung():
    for kw in ({}, {"remote_attempts": 1}, {"replacements_30d": 2}, {"chronic": True, "remote_attempts": 1}, {"daily_volume_paise": 9_000_000}):
        p = build_plan(S(**kw))
        c = costs(p)
        assert c[p.start_rung] == min(c.values())
        assert all(c[k] > c[p.start_rung] or k >= p.start_rung for k in c)               # nothing lower is as cheap


def test_the_last_rung_is_certain_and_every_chance_is_a_probability():
    for kw in ({}, {"remote_attempts": 1}, {"replacements_30d": 1}):
        p = build_plan(S(**kw))
        assert p.rungs[2].p_success == 1.0 and all(0 < r.p_success <= 1 for r in p.rungs)


def test_every_constant_behind_the_numbers_is_returned_with_the_plan():
    a = build_plan(S()).assumptions
    assert set(a) >= {"cost_paise", "hours", "p_base", "loss_share_bp", "shop_hours"} and a["shop_hours"] == "ASSUMED"


# ---------------------------------------------------------------------------------------------------- reading the situation from the books
@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    ensure_roster(w.conn)
    ensure_couriers(w.conn)
    return w


def test_the_situation_is_read_from_the_systems(world):
    s = read_situation(world.conn, world.clock, "M-0005")
    assert (s.device_status, s.language, s.remote_attempts, s.replacements_30d, s.chronic, s.hours_source) == ("OFFLINE", "hi-IN", 0, 0, False, "ASSUMED")
    assert s.silent_hours == 5 and s.daily_volume_paise == world.conn.execute("SELECT avg_daily_volume_paise FROM merchants WHERE merchant_id = 'M-0005'").fetchone()[0]


def test_a_recent_replacement_makes_it_a_repeat_and_a_cancelled_one_does_not(world):
    did = world.conn.execute("SELECT device_id FROM devices WHERE merchant_id = 'M-0005'").fetchone()[0]
    now = world.clock.now_iso()
    world.conn.execute("INSERT INTO device_replacements VALUES ('R-T1', ?, 'M-0005', 'x', 'CANCELLED', ?, ?)", (did, now, now))
    assert read_situation(world.conn, world.clock, "M-0005").replacements_30d == 0
    world.conn.execute("INSERT INTO device_replacements VALUES ('R-T2', ?, 'M-0005', 'x', 'CREATED', ?, ?)", (did, now, now))
    assert read_situation(world.conn, world.clock, "M-0005").replacements_30d == 1
    assert plan_fix(world.conn, world.clock, "M-0005")[1].start_rung == 2


def test_a_remote_attempt_on_file_counts_but_a_connectivity_check_does_not(world):
    from packs.paytm_merchant.mocks.remote import ensure
    ensure(world.conn)
    now = world.clock.now_iso()
    world.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-T1', 'M-0005', 'connectivity', ?, 'NO_CHANGE', NULL, 'DONE')", (now,))
    assert read_situation(world.conn, world.clock, "M-0005").remote_attempts == 0
    world.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-T2', 'M-0005', 'restart', ?, 'NO_CHANGE', NULL, 'DONE')", (now,))
    s = read_situation(world.conn, world.clock, "M-0005")
    assert s.remote_attempts == 1 and build_plan(s).start_rung == 3


def test_unknown_merchants_and_merchants_without_a_device_are_refused(world):
    with pytest.raises(KeyError):
        read_situation(world.conn, world.clock, "M-9999")
    world.conn.execute("DELETE FROM devices WHERE merchant_id = 'M-0005'")
    with pytest.raises(LookupError):
        read_situation(world.conn, world.clock, "M-0005")


def travel(world):
    return lambda a, b: ROAD.minutes(a, b, world.clock.now())


# ---------------------------------------------------------------------------------------------------- who does each rung
def test_a_first_fault_lists_munim_then_a_helper_then_a_technician_as_the_fallbacks(world):
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    steps = resolve_people(world.conn, world.clock, s, p, travel(world))["steps"]
    assert [x["rung"] for x in steps] == [1, 2, 3] and steps[0]["who"] == "munim"
    assert steps[1]["who"] == "helper" and steps[1]["booking"]["args"]["kind"] == "call" and steps[1]["booking"]["args"]["person_id"] == steps[1]["person_id"]
    assert steps[2]["who"] == "technician" and steps[2]["booking"]["args"]["kind"] == "visit"


def test_a_broken_device_is_swapped_by_a_courier_or_a_certified_person_not_a_technician_visit(world):
    from packs.paytm_merchant.mocks.remote import ensure
    ensure(world.conn)
    world.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-T3', 'M-0005', 'restart', ?, 'NO_CHANGE', NULL, 'DONE')", (world.clock.now_iso(),))
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    steps = resolve_people(world.conn, world.clock, s, p, travel(world))["steps"]
    assert [x["rung"] for x in steps] == [3] and steps[0]["who"] in ("courier", "certified_person")
    assert steps[0]["booking"]["args"]["kind"] == "visit" and steps[0]["cost_paise"] in (35_000, 80_000)


def test_with_no_courier_and_no_certified_person_in_range_a_technician_goes_and_the_step_says_why(world):
    from packs.paytm_merchant.mocks.remote import ensure
    ensure(world.conn)
    world.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-T4', 'M-0005', 'restart', ?, 'NO_CHANGE', NULL, 'DONE')", (world.clock.now_iso(),))
    world.conn.execute("UPDATE couriers SET units = 0")
    world.conn.execute("UPDATE technicians SET certifications_json = '[]'")
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    steps = resolve_people(world.conn, world.clock, s, p, travel(world))["steps"]
    assert "technician goes instead" in steps[0]["note"] and steps[0]["rejected"]


def test_a_visit_is_booked_when_the_shop_is_open(world):
    from packs.paytm_merchant.mocks.remote import ensure
    ensure(world.conn)
    world.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-T5', 'M-0005', 'restart', ?, 'NO_CHANGE', NULL, 'DONE')", (world.clock.now_iso(),))
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    slot = resolve_people(world.conn, world.clock, s, p, travel(world))["steps"][0]["booking"]["args"]["slot_at"]
    from core.engine.clock import parse_iso
    from field.shop_hours import get_hours
    assert get_hours(world.conn, "M-0005").is_open(parse_iso(slot)) and parse_iso(slot) >= world.clock.now()


def test_nothing_is_listed_when_there_is_nothing_to_fix(world):
    world.conn.execute("UPDATE devices SET status = 'ONLINE', last_test_announcement_ok = 1 WHERE merchant_id = 'M-0005'")
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    assert resolve_people(world.conn, world.clock, s, p, travel(world)) == {"start_rung": 0, "steps": []}


def test_a_merchant_with_no_position_gets_no_visit_and_the_step_says_so(world):
    from packs.paytm_merchant.mocks.remote import ensure
    ensure(world.conn)
    world.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-T6', 'M-0005', 'restart', ?, 'NO_CHANGE', NULL, 'DONE')", (world.clock.now_iso(),))
    world.conn.execute("UPDATE merchants SET lat = NULL WHERE merchant_id = 'M-0005'")
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    assert resolve_people(world.conn, world.clock, s, p, travel(world))["steps"][-1]["unavailable"] == "the merchant has no position on record"


def test_apply_booking_goes_through_the_executor_with_a_deterministic_key_so_a_retry_books_once(world):
    s, p = plan_fix(world.conn, world.clock, "M-0005")
    step = resolve_people(world.conn, world.clock, s, p, travel(world))["steps"][1]
    first = apply_booking(world.executor, step)
    again = apply_booking(world.executor, step)
    assert first.status == "OK" and again.status == "REPLAYED" and first.action_id == again.action_id
    assert world.conn.execute("SELECT COUNT(*) FROM bookings WHERE merchant_id = 'M-0005' AND kind = 'call'").fetchone()[0] == 1


def test_a_step_with_nothing_to_book_is_refused(world):
    with pytest.raises(ValueError):
        apply_booking(world.executor, {"rung": 1, "who": "munim"})


# ---------------------------------------------------------------------------------------------------- the helpers
def test_the_roster_is_seeded_once_and_a_second_call_changes_nothing(world):
    before = [h.helper_id for h in load_helpers(world.conn)]
    ensure_roster(world.conn)
    assert before == [h.helper_id for h in load_helpers(world.conn)] == ["H-01", "H-02", "H-03", "H-04"]


def at(hhmm: str) -> datetime:
    return DAY.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))


def test_a_kannada_speaker_gets_the_kannada_helper_who_is_on_call(world):
    r = choose_helper(world.conn, "kn-IN", at("10:00"))
    assert r.choice.helper_id == "H-02" and r.rejected["H-04"] == "NOT_ON_CALL"                # Karthik speaks it too, but is not on call until 14:00


def test_the_native_speaker_beats_a_fluent_one(world):
    assert choose_helper(world.conn, "hi-IN", at("10:00")).choice.helper_id == "H-01"          # Asha is native; Ravi and Sneha are fluent


def test_hinglish_is_served_by_anyone_fluent_in_hindi_or_english(world):
    assert choose_helper(world.conn, "hinglish", at("10:00")).choice.helper_id == "H-01"


def test_video_is_only_offered_by_helpers_who_do_video_and_a_tie_goes_to_the_lighter_day(world):
    r = choose_helper(world.conn, "en-IN", at("15:00"), mode="video")
    assert r.choice.helper_id == "H-01" and r.rejected["H-03"] == "MODE"                       # Sneha is native in English but voice only
    world.conn.execute("INSERT INTO bookings VALUES ('BK-T1', 'M-0001', 'call', 'H-01', '2026-10-31T16:00:00+05:30', 'BOOKED', ?)", (world.clock.now_iso(),))
    assert choose_helper(world.conn, "en-IN", at("15:00"), mode="video").choice.helper_id == "H-04"


def test_a_cancelled_call_does_not_count_against_a_helper(world):
    world.conn.execute("INSERT INTO bookings VALUES ('BK-T2', 'M-0001', 'call', 'H-01', '2026-10-31T16:00:00+05:30', 'CANCELLED', ?)", (world.clock.now_iso(),))
    assert choose_helper(world.conn, "en-IN", at("15:00"), mode="video").choice.helper_id == "H-01"


def test_at_night_nobody_is_on_call_and_every_reason_is_given(world):
    r = choose_helper(world.conn, "hi-IN", at("03:00"))
    assert r.choice is None and len(r.rejected) == 4
    assert r.rejected["H-01"] == r.rejected["H-02"] == r.rejected["H-03"] == "NOT_ON_CALL" and r.rejected["H-04"] == "LANGUAGE"        # Karthik does not speak Hindi at all


def test_an_unknown_mode_is_refused(world):
    with pytest.raises(ValueError):
        choose_helper(world.conn, "hi-IN", at("10:00"), mode="telepathy")


# ---------------------------------------------------------------------------------------------------- the swap
def test_a_courier_is_cheaper_than_a_certified_person_so_the_courier_comes(world):
    r = choose_swap(world.conn, (12.9401, 77.64178), at("10:00"), lambda a, b: ROAD.minutes(a, b, at("10:00")))
    assert r.choice.kind == "courier" and r.choice.person_id in ("CR-01", "CR-02") and r.choice.cost_paise == COURIER_COST_PAISE


def test_the_courier_nearest_the_shop_is_chosen_up_country(world):
    r = choose_swap(world.conn, (15.344, 75.095), at("10:00"), lambda a, b: ROAD.minutes(a, b, at("10:00")))
    assert r.choice.person_id == "CR-03" and r.rejected["CR-01"] == "TOO_FAR"


def test_with_no_courier_unit_a_certified_person_with_a_unit_comes(world):
    world.conn.execute("UPDATE couriers SET units = 0")
    r = choose_swap(world.conn, (12.9401, 77.64178), at("10:00"), lambda a, b: ROAD.minutes(a, b, at("10:00")))
    assert r.choice.kind == "certified_person" and r.rejected["CR-01"] == "NO_UNIT"


def test_an_uncertified_person_never_swaps_however_close(world):
    world.conn.execute("UPDATE couriers SET units = 0")
    world.conn.execute("UPDATE technicians SET certifications_json = '[]'")
    r = choose_swap(world.conn, (12.9401, 77.64178), at("10:00"), lambda a, b: 5.0)
    assert r.choice is None and "NOT_CERTIFIED" in r.rejected.values()


def test_nobody_swaps_outside_their_shift(world):
    r = choose_swap(world.conn, (12.9401, 77.64178), at("03:00"), lambda a, b: 5.0)
    assert r.choice is None and set(r.rejected.values()) <= {"OFF_SHIFT", "NOT_CERTIFIED"} and r.rejected["CR-01"] == "OFF_SHIFT"


def test_the_simulated_road_is_slower_in_the_peak_and_never_below_three_minutes():
    a, b = (12.95, 77.60), (12.99, 77.70)
    assert ROAD.minutes(a, b, at("09:00")) > ROAD.minutes(a, b, at("13:00")) and ROAD.minutes(a, a, at("13:00")) == 3.0
