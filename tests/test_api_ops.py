"""Service-ops API: the fix plan, the field day with its two exceptions, people, shop hours and the scam sandbox."""
import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from field.helpers import ensure_roster
from field.swap import ensure_couriers
from tests.checkpoint_script import SCRIPT
from tests.world import World

DAY = "2026-10-31"


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    ensure_roster(w.conn)
    ensure_couriers(w.conn)
    for bid, mid, hhmm in (("BK-A1", "M-0003", "13:00"), ("BK-A2", "M-0009", "14:30"), ("BK-A3", "M-0012", "15:30")):
        w.conn.execute("INSERT INTO bookings VALUES (?,?,'visit',NULL,?,'BOOKED',?)", (bid, mid, f"{DAY}T{hhmm}:00+05:30", w.clock.now_iso()))
    return w


@pytest.fixture
def api(world):
    return TestClient(create_api(world.db_path, world.clock, world.executor, world.http))


def break_device(w, mid="M-0005"):
    from packs.paytm_merchant.mocks.remote import ensure
    ensure(w.conn)
    w.conn.execute("INSERT INTO device_remote_ops VALUES ('RO-API1', ?, 'restart', ?, 'NO_CHANGE', NULL, 'DONE')", (mid, w.clock.now_iso()))


# ---------------------------------------------------------------------------------------------------- the fix plan
def test_a_first_fault_starts_on_rung_one_and_lists_the_fallbacks(api):
    d = api.get("/api/ops/fix-plan/M-0005").json()
    assert d["simulated"] is True and d["plan"]["start_rung"] == 1 and d["situation"]["device_status"] == "OFFLINE"
    assert [s["rung"] for s in d["people"]["steps"]] == [1, 2, 3] and [r["rung"] for r in d["plan"]["rungs"]] == [1, 2, 3]


def test_the_plan_carries_the_numbers_and_the_assumptions_behind_them(api):
    p = api.get("/api/ops/fix-plan/M-0005").json()["plan"]
    assert all({"cost_paise", "hours", "p_success", "expected_if_start_here_paise"} <= set(r) for r in p["rungs"])
    assert p["loss_per_hour_paise"] > 0 and p["assumptions"]["loss_share_bp"] == 500 and p["reasons"]


def test_a_broken_device_is_planned_as_a_swap_by_a_courier_or_certified_person(world, api):
    break_device(world)
    d = api.get("/api/ops/fix-plan/M-0005").json()
    assert d["plan"]["start_rung"] == 3 and d["plan"]["swap"] is True
    (step,) = d["people"]["steps"]
    assert step["who"] in ("courier", "certified_person") and step["booking"]["args"]["kind"] == "visit"


def test_a_healthy_device_has_nothing_to_fix(world, api):
    world.conn.execute("UPDATE devices SET status = 'ONLINE', last_test_announcement_ok = 1 WHERE merchant_id = 'M-0005'")
    d = api.get("/api/ops/fix-plan/M-0005").json()
    assert d["plan"]["start_rung"] == 0 and d["people"]["steps"] == []


def test_an_unknown_merchant_and_a_merchant_with_no_device_are_404(world, api):
    assert api.get("/api/ops/fix-plan/M-9999").json()["detail"] == "MERCHANT_NOT_FOUND"
    world.conn.execute("DELETE FROM devices WHERE merchant_id = 'M-0005'")
    assert api.get("/api/ops/fix-plan/M-0005").json()["detail"] == "NO_DEVICE"


def test_booking_a_planned_step_goes_through_the_executor_and_a_second_press_books_nothing_new(world, api):
    r = api.post("/api/ops/fix-plan/M-0005/book", json={"rung": 2}).json()
    again = api.post("/api/ops/fix-plan/M-0005/book", json={"rung": 2}).json()
    assert r["status"] == "OK" and again["status"] == "REPLAYED" and r["action_id"] == again["action_id"] and r["step"]["booking"]["args"]["kind"] == "call"
    assert world.conn.execute("SELECT COUNT(*) FROM bookings WHERE merchant_id = 'M-0005' AND kind = 'call'").fetchone()[0] == 1


def test_rung_one_has_nothing_to_book_and_an_unknown_rung_is_refused(api):
    assert api.post("/api/ops/fix-plan/M-0005/book", json={"rung": 1}).status_code == 422
    assert api.post("/api/ops/fix-plan/M-9999/book", json={"rung": 3}).status_code == 404


def test_no_helper_on_call_means_nothing_to_book_and_the_reason_is_given(world, api):
    world.clock.advance(__import__("datetime").timedelta(hours=16))                                  # 03:00: nobody is on call
    r = api.post("/api/ops/fix-plan/M-0005/book", json={"rung": 2})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "NOTHING_TO_BOOK"


def test_looking_at_a_plan_books_nothing(world, api):
    before = world.conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    api.get("/api/ops/fix-plan/M-0005")
    assert world.conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == before


# ---------------------------------------------------------------------------------------------------- the field day
def test_the_day_is_planned_for_everyone_and_marked_simulated(api):
    d = api.get("/api/ops/day").json()
    placed = {s["job_id"] for route in d["routes"].values() for s in route} | {u["job_id"] for u in d["unassigned"]}
    assert placed == {"BK-A1", "BK-A2", "BK-A3"} and d["simulated"] is True and d["day"] == DAY


def test_reading_the_day_writes_no_route(world, api):
    api.get("/api/ops/day")
    assert world.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 0


def test_a_closed_shop_is_recorded_ledgered_and_the_day_is_replanned_in_the_same_call(world, api):
    r = api.post("/api/ops/day/exception", json={"kind": "SHOP_CLOSED", "booking_id": "BK-A1"})
    assert r.status_code == 200
    body = r.json()
    assert body["exception"]["kind"] == "SHOP_CLOSED" and [x["booking_id"] for x in body["plan"]["rescheduled"]] == ["BK-A1"]
    assert "BK-A1" not in {s["job_id"] for route in body["plan"]["routes"].values() for s in route}
    assert world.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] >= 1                    # the re-plan is saved
    human = [e for e in world.ledger.entries() if e.kind == "HUMAN_ACTION" and e.payload.get("action") == "field_day_exception"]
    assert human and human[-1].payload["kind"] == "SHOP_CLOSED"


def test_a_visit_running_long_is_recorded_and_replans(world, api):
    r = api.post("/api/ops/day/exception", json={"kind": "RUNNING_LONG", "person_id": "T-01", "minutes": 90})
    assert r.status_code == 200 and r.json()["plan"]["delayed"] == {"T-01": 90}


@pytest.mark.parametrize("body,code", [
    ({"kind": "SHOP_CLOSED", "booking_id": "BK-NOPE"}, 404),
    ({"kind": "RUNNING_LONG", "person_id": "T-99", "minutes": 30}, 404),
    ({"kind": "RUNNING_LONG", "person_id": "T-01", "minutes": 0}, 422),
    ({"kind": "RUNNING_LONG", "person_id": "T-01", "minutes": 100000}, 422),
    ({"kind": "ON_FIRE"}, 422),
])
def test_a_bad_exception_is_refused_and_nothing_is_recorded(world, api, body, code):
    assert api.post("/api/ops/day/exception", json=body).status_code == code
    try:
        n = world.conn.execute("SELECT COUNT(*) FROM visit_exceptions").fetchone()[0]
    except Exception:
        n = 0
    assert n == 0


def test_the_proposed_new_visit_is_booked_through_the_executor_only_after_the_shop_was_reported_closed(world, api):
    assert api.post("/api/ops/day/reschedule", json={"booking_id": "BK-A1"}).status_code == 409
    api.post("/api/ops/day/exception", json={"kind": "SHOP_CLOSED", "booking_id": "BK-A1"})
    r = api.post("/api/ops/day/reschedule", json={"booking_id": "BK-A1"}).json()
    assert r["status"] == "OK" and r["proposal"]["booking_id"] == "BK-A1"
    assert world.conn.execute("SELECT COUNT(*) FROM bookings WHERE merchant_id = 'M-0003' AND slot_at = ?", (r["proposal"]["proposed_slot_at"],)).fetchone()[0] == 1


def test_rescheduling_an_unknown_booking_is_404(api):
    assert api.post("/api/ops/day/reschedule", json={"booking_id": "BK-NOPE"}).status_code == 404


# ---------------------------------------------------------------------------------------------------- people
def test_the_people_screen_has_technicians_helpers_couriers_and_positions(api):
    d = api.get("/api/ops/people").json()
    assert len(d["technicians"]) == 8 and [h["helper_id"] for h in d["helpers"]] == ["H-01", "H-02", "H-03", "H-04"] and len(d["couriers"]) == 3
    assert d["positions"] == [] and d["sim_time"].startswith(DAY)
    t = d["technicians"][0]
    assert {"tech_id", "name", "languages", "skills", "certifications", "parts", "shift", "base", "max_jobs_per_day"} <= set(t) and ":" in t["shift"][0]
    assert json.loads(json.dumps(d)) == d


def test_the_people_screen_is_empty_not_broken_before_anyone_is_on_the_roster(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    d = TestClient(create_api(w.db_path, w.clock, w.executor, w.http)).get("/api/ops/people").json()
    assert d["helpers"] == [] and d["couriers"] == []


def test_a_position_is_refused_without_an_active_route_and_stored_once_there_is_one(world, api):
    r = api.post("/api/ops/people/T-01/position", json={"lat": 12.93, "lng": 77.62}).json()
    assert r == {"accepted": False, "reason": "NO_ACTIVE_ROUTE"} and api.get("/api/ops/people").json()["positions"] == []
    api.post("/api/ops/day/exception", json={"kind": "RUNNING_LONG", "person_id": "T-01", "minutes": 10})          # re-plans and saves today's routes
    tech = world.conn.execute("SELECT person_id FROM routes ORDER BY person_id LIMIT 1").fetchone()[0]
    ok = api.post(f"/api/ops/people/{tech}/position", json={"lat": 12.93, "lng": 77.62}).json()
    assert ok["accepted"] is True
    (p,) = api.get("/api/ops/people").json()["positions"]
    assert p["person_id"] == tech and p["age_min"] == 0


def test_a_position_for_nobody_or_nowhere_is_refused(api):
    assert api.post("/api/ops/people/T-99/position", json={"lat": 12.9, "lng": 77.6}).status_code == 404
    assert api.post("/api/ops/people/T-01/position", json={"lat": 912.9, "lng": 77.6}).status_code == 422
    assert api.post("/api/ops/people/T-01/position", json={"kind": "boss", "lat": 12.9, "lng": 77.6}).status_code == 422


# ---------------------------------------------------------------------------------------------------- shop hours
def test_shop_hours_default_to_a_typical_day_marked_assumed(api):
    assert api.get("/api/ops/shop-hours/M-0001").json() == {"open": "08:00", "close": "21:00", "closed_days": [], "source": "ASSUMED"}


def test_reported_hours_are_saved_ledgered_and_shown_as_reported(world, api):
    r = api.put("/api/ops/shop-hours/M-0001", json={"open": "09:30", "close": "18:00", "closed_days": ["sun"]})
    assert r.status_code == 200 and r.json() == {"open": "09:30", "close": "18:00", "closed_days": ["sun"], "source": "REPORTED"}
    assert api.get("/api/ops/shop-hours/M-0001").json()["source"] == "REPORTED"
    assert any(e.kind == "HUMAN_ACTION" and e.payload.get("action") == "shop_hours" for e in world.ledger.entries())


@pytest.mark.parametrize("body", [{"open": "9am", "close": "18:00"}, {"open": "18:00", "close": "09:00"}, {"open": "09:00", "close": "18:00", "closed_days": ["funday"]}])
def test_bad_hours_are_422_and_change_nothing(api, body):
    assert api.put("/api/ops/shop-hours/M-0001", json=body).status_code == 422
    assert api.get("/api/ops/shop-hours/M-0001").json()["source"] == "ASSUMED"


def test_hours_for_an_unknown_merchant_are_404(api):
    assert api.get("/api/ops/shop-hours/M-9999").status_code == 404 and api.put("/api/ops/shop-hours/M-9999", json={"open": "09:00", "close": "18:00"}).status_code == 404


# ---------------------------------------------------------------------------------------------------- the scam sandbox
def test_a_real_payment_checks_out_over_the_api(world, api):
    p = world.conn.execute("SELECT * FROM payments WHERE merchant_id = 'M-0002' AND status = 'SUCCESS' AND reference IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    r = api.post("/api/ops/scam-check", json={"merchant_id": "M-0002", "amount_paise": p["amount_paise"], "reference": p["reference"], "claimed_at": p["ts"]}).json()
    assert r["verdict"] == "REAL" and r["discarded"] is True


def test_a_badly_shaped_reference_is_a_likely_scam_over_the_api(api):
    r = api.post("/api/ops/scam-check", json={"merchant_id": "M-0002", "amount_paise": 5000, "reference": "PAID-OK"}).json()
    assert r["verdict"] == "LIKELY_SCAM" and "REFERENCE_FORMAT" in {s["code"] for s in r["signals"]}


def test_the_scam_check_changes_nothing(world, api):
    before = world.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    api.post("/api/ops/scam-check", json={"merchant_id": "M-0002", "amount_paise": 5000})
    assert (world.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == before


@pytest.mark.parametrize("body,code", [
    ({"merchant_id": "M-9999", "amount_paise": 100}, 404),
    ({"merchant_id": "M-0002", "amount_paise": 0}, 422),
    ({"merchant_id": "M-0002", "amount_paise": -5}, 422),
])
def test_a_bad_scam_check_is_refused(api, body, code):
    assert api.post("/api/ops/scam-check", json=body).status_code == code
