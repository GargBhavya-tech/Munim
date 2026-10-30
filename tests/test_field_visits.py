"""#111 visit logging and re-routing: voice note -> validated fields; a visit counts only with merchant confirmation, a nearby geotag and a photo."""
import json
from datetime import timedelta

import pytest

from field.privacy import FieldPrivacy
from field.profiles import NotAllowed
from field.routing import Stop, Vehicle
from field.visits import CONFIRMED, FLAGGED, LOGGED, VisitFieldsInvalid, VisitLog, metres, reroute_after_visit, validate_fields

SHOP = (12.9900, 77.5500)
NOTE = "Visited the shop, owner agreed to a soundbox, order for 2 units, value 4500 rupees."
GOOD = {"outcome": "CONVERTED", "units": 2, "order_value_paise": 450000}


def shift_with_route(conn, clock, hhmm="11:00"):
    h, m = hhmm.split(":")
    clock.set(clock.now().replace(hour=int(h), minute=int(m), second=0))
    conn.execute("INSERT INTO routes VALUES (?,?,?,?,?,?)", ("RT-1", "R-01", "rep", clock.now().date().isoformat(), json.dumps({"active": True, "stops": []}), clock.now_iso()))


@pytest.fixture
def log(conn, clock):
    shift_with_route(conn, clock)
    return VisitLog(conn, clock, FieldPrivacy(conn, clock))


def make(log, **kw):
    args = dict(geotag=(12.9901, 77.5501), photo_ref="photo-sha-1")
    args.update(kw)
    return log.log_visit("R-01", "L-0001", SHOP, NOTE, lambda t: GOOD, **args)


# ---------------------------------------------------------------- structured fields
def test_fields_are_validated_and_numbers_must_be_in_the_note():
    assert validate_fields(NOTE, GOOD) == GOOD
    with pytest.raises(VisitFieldsInvalid):
        validate_fields(NOTE, {**GOOD, "order_value_paise": 500000})            # 5000 rupees was never said
    with pytest.raises(VisitFieldsInvalid):
        validate_fields(NOTE, {**GOOD, "units": 3})
    with pytest.raises(VisitFieldsInvalid):
        validate_fields(NOTE, {"outcome": "HAPPY"})
    with pytest.raises(VisitFieldsInvalid):
        validate_fields("order value 4,500", {"outcome": "CONVERTED", "order_value_paise": "450000"})     # not an int
    assert validate_fields("order value 4,500 rupees", {"outcome": "CONVERTED", "order_value_paise": 450000})["order_value_paise"] == 450000


def test_a_logged_visit_stores_structured_fields_and_the_transcript(log):
    v = make(log)
    assert v["fields"] == GOOD and v["transcript"] == NOTE and v["status"] == LOGGED and v["flags"] == [] and v["geo_ok"] == 1 and v["visit_id"] == "V-00001"


# ---------------------------------------------------------------- Watch out: fake visits accepted without confirmation
def test_a_visit_without_merchant_confirmation_is_flagged_and_never_counted(log, clock):
    v = make(log)
    assert log.verified_visits("R-01") == []                                     # logged but not confirmed: counts for nothing
    clock.advance(timedelta(hours=23, minutes=59))
    assert log.sweep_unconfirmed() == []
    clock.advance(timedelta(minutes=2))
    assert log.sweep_unconfirmed() == [v["visit_id"]]
    assert log.get(v["visit_id"])["status"] == FLAGGED and log.verified_visits("R-01") == []
    assert log.sweep_unconfirmed() == []                                         # once


def test_the_rep_cannot_confirm_their_own_visit(log):
    v = make(log)
    for who in ("R-01", "rep:R-01", "merchant:L-9999", "system"):
        with pytest.raises(NotAllowed):
            log.confirm(v["visit_id"], who)
    assert log.get(v["visit_id"])["status"] == LOGGED


def test_merchant_confirmation_counts_the_visit_when_geotag_and_photo_are_present(log):
    v = make(log)
    done = log.confirm(v["visit_id"], "merchant:L-0001")
    assert done["status"] == CONFIRMED and done["confirmed_at"] and [x["visit_id"] for x in log.verified_visits("R-01")] == [v["visit_id"]]
    assert log.confirm(v["visit_id"], "merchant:L-0001")["confirmed_at"] == done["confirmed_at"]            # idempotent


@pytest.mark.parametrize("kw,flag", [({"geotag": None}, "NO_GEOTAG"), ({"geotag": (13.05, 77.55)}, "GEO_MISMATCH"), ({"photo_ref": None}, "NO_PHOTO")])
def test_a_confirmed_visit_missing_geotag_or_photo_is_still_not_counted(log, kw, flag):
    v = make(log, **kw)
    assert v["flags"] == [flag]
    log.confirm(v["visit_id"], "merchant:L-0001")
    assert log.verified_visits("R-01") == []


def test_geotag_outside_the_shift_is_not_stored_and_flags_the_visit(conn, clock):
    shift_with_route(conn, clock, "22:30")
    lg = VisitLog(conn, clock, FieldPrivacy(conn, clock))
    v = make(lg)
    assert v["flags"] == ["NO_GEOTAG:OUTSIDE_SHIFT"] and conn.execute("SELECT COUNT(*) FROM field_locations").fetchone()[0] == 0


def test_a_good_geotag_is_recorded_through_the_privacy_module_for_the_owner_only(log, conn):
    make(log)
    assert conn.execute("SELECT person_id, route_id FROM field_locations").fetchall()[0]["route_id"] == "RT-1"
    assert metres(SHOP, (12.9901, 77.5501)) < 20 and metres(SHOP, (13.05, 77.55)) > 5000


# ---------------------------------------------------------------- messages go through the executor path
def test_requesting_confirmation_is_an_executor_intent_not_a_direct_send(log):
    v = make(log)
    seen = []
    log.request_confirmation(v["visit_id"], "Did our representative visit you today?", lambda i: seen.append(i) or "ok")
    (i,) = seen
    assert i.action == "send_message" and i.proactive and i.allowlist == ("send_message",) and i.args["merchant_id"] == "L-0001" and i.facts == {"visit_id": v["visit_id"]}


def test_visits_carry_no_coaching_content(log, conn):
    make(log)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(field_visits)")}
    assert not any(w in c for c in cols for w in ("coach", "score", "rank", "penalty", "religion", "caste", "community"))


# ---------------------------------------------------------------- re-routing
MATRIX = [[0, 10, 20, 30], [10, 0, 10, 20], [20, 10, 0, 10], [30, 20, 10, 0]]
STOPS = [Stop("A", 1, 100, 10), Stop("B", 2, 100, 10), Stop("C", 3, 100, 10)]
VEH = [Vehicle("R-01", 0, 0, 0, 600)]


def test_a_closed_shop_or_finished_visit_leaves_the_plan_and_the_rest_is_replanned_from_where_the_rep_is():
    plan = reroute_after_visit(MATRIX, STOPS, VEH, stop_id="A", outcome="SHOP_CLOSED", now=60, position_node=1, vehicle_id="R-01")
    ids = plan.visited()
    assert "A" not in ids and sorted(ids) == ["B", "C"] and plan.routes["R-01"][0].stop_id == "B" and plan.routes["R-01"][0].arrive == 70
    done = reroute_after_visit(MATRIX, STOPS, VEH, stop_id="A", outcome="CONVERTED", now=60, position_node=1, vehicle_id="R-01")
    assert "A" not in done.visited()


def test_a_rescheduled_stop_comes_back_no_earlier_than_the_new_time():
    plan = reroute_after_visit(MATRIX, STOPS, VEH, stop_id="A", outcome="RESCHEDULED", now=60, position_node=1, vehicle_id="R-01", new_earliest=300)
    visit = next(v for v in plan.routes["R-01"] if v.stop_id == "A")
    assert visit.arrive >= 300 and set(plan.visited()) == {"A", "B", "C"}
    with pytest.raises(ValueError):
        reroute_after_visit(MATRIX, STOPS, VEH, stop_id="A", outcome="RESCHEDULED", now=60, position_node=1, vehicle_id="R-01")
    with pytest.raises(ValueError):
        reroute_after_visit(MATRIX, STOPS, VEH, stop_id="A", outcome="LOVED_IT", now=60, position_node=1, vehicle_id="R-01")
