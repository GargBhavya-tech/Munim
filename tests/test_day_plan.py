"""The whole day for everyone, a closed shop, a visit running long, and where people are right now."""
import json
from datetime import timedelta

import pytest

from field.day_plan import MAX_DELAY_MIN, SERVICE_MIN, apply_reschedule, plan_day, record_exception
from field.live import latest_positions, report_position
from field.shop_hours import get_hours, to_minutes
from field.simulated_travel import SimulatedRoadProvider
from tests.checkpoint_script import SCRIPT
from tests.world import World

ROAD = SimulatedRoadProvider()
DAY = "2026-10-31"
VISITS = (("BK-T1", "M-0003", "13:00"), ("BK-T2", "M-0009", "14:30"), ("BK-T3", "M-0012", "15:30"), ("BK-T4", "M-0015", "16:30"))      # four Bengaluru shops


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    for bid, mid, hhmm in VISITS:
        w.conn.execute("INSERT INTO bookings VALUES (?,?,'visit',NULL,?,'BOOKED',?)", (bid, mid, f"{DAY}T{hhmm}:00+05:30", w.clock.now_iso()))
    return w


def plan(w, **kw):
    return plan_day(w.conn, w.clock, ROAD.minutes, simulated=True, **kw)


def stops(p):
    return [s for route in p.routes.values() for s in route]


def test_every_visit_goes_to_exactly_one_person_and_none_is_lost(world):
    p = plan(world)
    ids = sorted([s.job_id for s in stops(p)] + [u["job_id"] for u in p.unassigned])
    assert ids == sorted(b for b, _, _ in VISITS)


def test_each_person_planned_has_the_skill_and_a_unit_and_is_within_their_shift(world):
    from field.profiles import load_techs
    techs = {t.tech_id: t for t in load_techs(world.conn)}
    p = plan(world)
    assert p.routes and p.simulated is True
    for tech_id, route in p.routes.items():
        t = techs[tech_id]
        assert "soundbox_repair" in t.skills and t.parts.get("soundbox_unit", 0) >= 1
        assert all(t.shift[0] <= s.arrive_min and s.leave_min <= t.shift[1] for s in route)


def test_every_stop_takes_the_service_time_and_falls_inside_the_shop_hours(world):
    for s in stops(plan(world)):
        h = get_hours(world.conn, s.merchant_id)
        assert s.leave_min - s.arrive_min == SERVICE_MIN and h.open_min <= s.arrive_min and s.leave_min <= h.close_min and s.late_min == 0


def test_nothing_is_planned_in_the_past(world):
    now_min = world.clock.now().hour * 60 + world.clock.now().minute
    assert all(s.arrive_min >= now_min for s in stops(plan(world)))


def test_the_plan_is_the_same_every_time_for_the_same_facts(world):
    assert plan(world).as_dict() == plan(world).as_dict()


def test_a_visit_nobody_can_take_is_returned_with_the_reason_for_each_person(world):
    world.conn.execute("UPDATE technicians SET skills_json = '[\"installation\"]'")
    p = plan(world)
    assert not p.routes and {u["job_id"] for u in p.unassigned} == {b for b, _, _ in VISITS}
    assert all(u["reason"] == "NO_ELIGIBLE_TECHNICIAN" and set(u["rejected"].values()) == {"SKILL"} for u in p.unassigned)


def test_a_booking_for_another_day_or_already_done_is_not_planned(world):
    world.conn.execute("UPDATE bookings SET status = 'CANCELLED' WHERE booking_id = 'BK-T1'")
    world.conn.execute("UPDATE bookings SET slot_at = '2026-11-02T13:00:00+05:30' WHERE booking_id = 'BK-T2'")
    ids = {s.job_id for s in stops(plan(world))} | {u["job_id"] for u in plan(world).unassigned}
    assert ids == {"BK-T3", "BK-T4"}


def test_calls_are_not_visits_and_are_not_planned(world):
    world.conn.execute("INSERT INTO bookings VALUES ('BK-C1', 'M-0003', 'call', 'H-01', ?, 'BOOKED', ?)", (f"{DAY}T12:00:00+05:30", world.clock.now_iso()))
    assert "BK-C1" not in {s.job_id for s in stops(plan(world))}


# ---------------------------------------------------------------------------------------------------- a shop is closed
def test_a_closed_shop_leaves_todays_plan_and_comes_back_as_a_proposal_for_its_next_opening(world):
    record_exception(world.conn, world.clock, "SHOP_CLOSED", booking_id="BK-T1")
    p = plan(world)
    assert "BK-T1" not in {s.job_id for s in stops(p)}
    (r,) = p.rescheduled
    assert r["booking_id"] == "BK-T1" and r["reason"] == "SHOP_CLOSED" and r["merchant_id"] == "M-0003"
    assert r["proposed_slot_at"].startswith("2026-11-01T11:00")                      # M-0003 is a restaurant: it opens at 11:00, the next day


def test_the_other_visits_are_still_planned_when_one_shop_is_closed(world):
    record_exception(world.conn, world.clock, "SHOP_CLOSED", booking_id="BK-T1")
    assert {s.job_id for s in stops(plan(world))} | {u["job_id"] for u in plan(world).unassigned} == {"BK-T2", "BK-T3", "BK-T4"}


def test_a_closed_shop_that_opens_only_after_a_closed_day_is_proposed_after_it(world, clock):
    from field.shop_hours import set_hours
    set_hours(world.conn, clock, "M-0003", "11:00", "23:00", ("sun",))
    record_exception(world.conn, world.clock, "SHOP_CLOSED", booking_id="BK-T1")
    assert plan(world).rescheduled[0]["proposed_slot_at"].startswith("2026-11-02T11:00")           # Sunday the 1st is skipped


def test_a_visit_to_a_shop_that_is_closed_that_whole_day_is_not_planned_and_says_why(world, clock):
    from field.shop_hours import set_hours
    set_hours(world.conn, clock, "M-0003", "11:00", "23:00", ("sat",))                              # 31 October is a Saturday
    p = plan(world)
    assert {"job_id": "BK-T1", "merchant_id": "M-0003", "reason": "SHOP_CLOSED_THAT_DAY"} in p.unassigned


# ---------------------------------------------------------------------------------------------------- a visit runs long
def test_a_person_running_long_does_not_start_their_remaining_visits_before_they_are_free(world):
    base = plan(world)
    tech = sorted(base.routes)[0]
    free_at = world.clock.now().hour * 60 + world.clock.now().minute + 120
    record_exception(world.conn, world.clock, "RUNNING_LONG", person_id=tech, minutes=120)
    p = plan(world)
    assert p.delayed == {tech: 120} and all(s.arrive_min >= free_at for s in p.routes.get(tech, []))


def test_delays_add_up(world):
    record_exception(world.conn, world.clock, "RUNNING_LONG", person_id="T-01", minutes=30)
    record_exception(world.conn, world.clock, "RUNNING_LONG", person_id="T-01", minutes=45)
    assert plan(world).delayed == {"T-01": 75}


# ---------------------------------------------------------------------------------------------------- recording exceptions
@pytest.mark.parametrize("kw", [{"kind": "SHOP_CLOSED", "booking_id": "BK-NOPE"}, {"kind": "RUNNING_LONG", "person_id": "T-99", "minutes": 30}])
def test_an_unknown_booking_or_person_is_refused(world, kw):
    with pytest.raises(KeyError):
        record_exception(world.conn, world.clock, **kw)


@pytest.mark.parametrize("minutes", [0, -5, MAX_DELAY_MIN + 1, 2.5, None])
def test_a_delay_must_be_a_sensible_whole_number_of_minutes(world, minutes):
    with pytest.raises(ValueError):
        record_exception(world.conn, world.clock, "RUNNING_LONG", person_id="T-01", minutes=minutes)


def test_an_unknown_kind_is_refused_and_a_call_is_not_a_visit_that_can_be_closed(world):
    with pytest.raises(ValueError):
        record_exception(world.conn, world.clock, "ON_FIRE", booking_id="BK-T1")
    world.conn.execute("INSERT INTO bookings VALUES ('BK-C2', 'M-0003', 'call', 'H-01', ?, 'BOOKED', ?)", (f"{DAY}T12:00:00+05:30", world.clock.now_iso()))
    with pytest.raises(KeyError):
        record_exception(world.conn, world.clock, "SHOP_CLOSED", booking_id="BK-C2")


def test_an_exception_is_ledgered_with_only_the_facts(world):
    record_exception(world.conn, world.clock, "RUNNING_LONG", person_id="T-01", minutes=20, ledger=world.ledger)
    e = [x for x in world.ledger.entries() if x.payload.get("field_day")][-1]
    assert e.payload == {"field_day": "RUNNING_LONG", "booking_id": None, "person_id": "T-01", "minutes": 20}


def test_booking_the_proposed_visit_goes_through_the_executor_once(world):
    record_exception(world.conn, world.clock, "SHOP_CLOSED", booking_id="BK-T1")
    proposal = plan(world).rescheduled[0]
    first, again = apply_reschedule(world.executor, proposal), apply_reschedule(world.executor, proposal)
    assert first.status == "OK" and again.status == "REPLAYED"
    row = world.conn.execute("SELECT * FROM bookings WHERE merchant_id = 'M-0003' AND slot_at = ?", (proposal["proposed_slot_at"],)).fetchall()
    assert len(row) == 1 and row[0]["kind"] == "visit"


# ---------------------------------------------------------------------------------------------------- persisting, and where people are
def test_persisting_writes_one_route_per_person_marked_active_for_today(world):
    p = plan(world, persist=True)
    rows = world.conn.execute("SELECT * FROM routes WHERE route_date = ?", (DAY,)).fetchall()
    assert sorted(r["person_id"] for r in rows) == sorted(p.routes)
    body = json.loads(rows[0]["plan_json"])
    assert body["active"] is True and body["simulated"] is True and body["stops"]


def test_a_dry_plan_writes_nothing(world):
    plan(world)
    assert world.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 0


def test_a_position_is_accepted_only_inside_the_shift_on_an_active_route(world):
    assert report_position(world.conn, world.clock, world.ledger, "T-01", "tech", 12.93, 77.62) == {"accepted": False, "reason": "NO_ACTIVE_ROUTE"}
    plan(world, persist=True)
    tech = sorted(plan(world).routes)[0]
    assert report_position(world.conn, world.clock, world.ledger, tech, "tech", 12.93, 77.62)["accepted"] is True
    world.clock.advance(timedelta(hours=9))
    assert report_position(world.conn, world.clock, world.ledger, tech, "tech", 12.93, 77.62) == {"accepted": False, "reason": "OUTSIDE_SHIFT"}


def test_the_latest_position_is_shown_while_recent_and_dropped_when_stale(world):
    plan(world, persist=True)
    tech = sorted(plan(world).routes)[0]
    assert latest_positions(world.conn, world.clock) == []
    report_position(world.conn, world.clock, world.ledger, tech, "tech", 12.93, 77.62)
    (p,) = latest_positions(world.conn, world.clock)
    assert p["person_id"] == tech and p["age_min"] == 0 and p["name"]
    world.clock.advance(timedelta(minutes=45))
    assert latest_positions(world.conn, world.clock) == []


def test_a_person_with_a_live_position_is_planned_from_it_not_from_their_base(world):
    plan(world, persist=True)
    tech = sorted(plan(world).routes)[0]
    report_position(world.conn, world.clock, world.ledger, tech, "tech", 12.9401, 77.64178)      # standing at the first shop
    assert plan(world).starts[tech] == "live position"
    assert plan(world).routes[tech][0].arrive_min <= plan(world, now_min=0).routes[tech][0].arrive_min + 60
