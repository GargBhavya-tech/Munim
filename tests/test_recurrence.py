"""#95 repeat issues and root causes: CHRONIC flag, one root-cause task for a senior with history, and fleet grouping by pincode / bank."""
import json
from datetime import timedelta

from core.engine.clock import iso
from core.engine.events import append_event
from jobs.recurrence import RecurrenceJob, classify_root_cause

MID = "M-0010"


def add_case(conn, clock, n, days_ago, pb="C2_SOUNDBOX_FAULT", mid=MID, state="RESOLVED"):
    conn.execute("INSERT INTO cases(case_id, merchant_id, playbook_id, state, lane, opened_at, resume_note) VALUES (?,?,?,?,?,?,?)",
                 (f"CASE-R{mid}-{n}", mid, pb, state, "service", iso(clock.now() - timedelta(days=days_ago)), f"fix attempt {n}"))
    conn.commit()


def test_third_c2_in_30_days_sets_chronic_and_routes_to_a_senior_with_history(conn, clock):
    add_case(conn, clock, 1, 25); add_case(conn, clock, 2, 12); add_case(conn, clock, 3, 0, state="ACTIVE")
    conn.execute("UPDATE devices SET signal_pct=90 WHERE merchant_id=?", (MID,))
    job = RecurrenceJob(conn, clock, senior_pool=("SENIOR-A", "SENIOR-B"))
    rep = job.run([MID])
    assert (MID, "C2_SOUNDBOX_FAULT") in rep.chronic and job.is_chronic(MID)
    flag = conn.execute("SELECT * FROM chronic_flags WHERE merchant_id=? AND issue_type='C2_SOUNDBOX_FAULT'", (MID,)).fetchone()
    assert flag["occurrences"] == 3 and flag["owner_tier"] == "SENIOR" and flag["owner_id"] in ("SENIOR-A", "SENIOR-B") and flag["monitoring"] == 1
    task = conn.execute("SELECT * FROM root_cause_tasks WHERE merchant_id=?", (MID,)).fetchone()
    assert task["owner_tier"] == "SENIOR" and task["owner_id"] == flag["owner_id"] and task["status"] == "OPEN"
    assert task["cause"] == "FAULTY_DEVICE" and task["action"].startswith("Replace the device")
    hist = json.loads(task["history_json"])
    assert [c["case_id"] for c in hist["cases"]] == [f"CASE-R{MID}-{i}" for i in (1, 2, 3)]
    assert hist["cases"][0]["resume_note"] == "fix attempt 1"
    w = conn.execute("SELECT * FROM monitoring_watch WHERE merchant_id=?", (MID,)).fetchone()
    assert w["active"] == 1 and "C2_SOUNDBOX_FAULT" in w["reason"]


def test_one_case_or_old_cases_are_not_chronic(conn, clock):
    add_case(conn, clock, 1, 5)
    add_case(conn, clock, 2, 40); add_case(conn, clock, 3, 31)      # outside the 30-day window
    job = RecurrenceJob(conn, clock)
    assert job.run([MID]).chronic == [] and not job.is_chronic(MID)
    assert conn.execute("SELECT COUNT(*) FROM root_cause_tasks").fetchone()[0] == 0


def test_different_issue_types_do_not_add_up(conn, clock):
    add_case(conn, clock, 1, 5, "C2_SOUNDBOX_FAULT"); add_case(conn, clock, 2, 4, "S1_ACTIVATION"); add_case(conn, clock, 3, 3, "C4_SETTLEMENT")
    assert RecurrenceJob(conn, clock).run([MID]).chronic == []


def test_second_occurrence_is_chronic_under_the_bibles_rule(conn, clock):
    add_case(conn, clock, 1, 20); add_case(conn, clock, 2, 1)
    assert RecurrenceJob(conn, clock).run([MID]).chronic == [(MID, "C2_SOUNDBOX_FAULT")]
    assert RecurrenceJob(conn, clock, min_occurrences=3).run(["M-0011"]).chronic == []


def test_repeats_are_not_new_tasks_each_time(conn, clock):
    """The Watch out: treating repeats as new tickets each time."""
    add_case(conn, clock, 1, 25); add_case(conn, clock, 2, 12)
    job = RecurrenceJob(conn, clock)
    job.run([MID]); job.run([MID])
    add_case(conn, clock, 3, 0)
    rep = job.run([MID])
    assert rep.tasks == []                                                          # an open task already exists
    assert conn.execute("SELECT COUNT(*) FROM root_cause_tasks WHERE merchant_id=?", (MID,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chronic_flags WHERE merchant_id=?", (MID,)).fetchone()[0] == 1
    assert conn.execute("SELECT occurrences FROM chronic_flags").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE merchant_id=?", (MID,)).fetchone()[0] == 0     # the job never raises tickets


def test_seeded_chronic_soundbox_m0012_same_device_failing_again(conn, clock):
    job = RecurrenceJob(conn, clock)
    rep = job.run(["M-0012"])
    assert ("M-0012", "DEVICE:D-0010") in rep.chronic
    t = conn.execute("SELECT * FROM root_cause_tasks WHERE merchant_id='M-0012'").fetchone()
    assert t["cause"] == "FAULTY_DEVICE" and len(json.loads(t["history_json"])["replacements"]) == 2


def test_cause_table(conn, clock):
    assert classify_root_cause({"fleet_merchants": 12, "same_device_repeat": True})[0] == "PAYTM_SIDE"
    assert classify_root_cause({"weak_signal": True, "same_device_repeat": True})[0] == "ENVIRONMENT"
    assert classify_root_cause({"same_device_repeat": True})[0] == "FAULTY_DEVICE"
    assert classify_root_cause({"how_to_repeat": True})[0] == "MERCHANT_NEEDS_HELP"
    assert classify_root_cause({})[0] == "UNKNOWN"
    assert classify_root_cause({"fleet_merchants": 9, "same_device_repeat": True})[0] == "FAULTY_DEVICE"     # 9 is not many


def test_weak_signal_at_the_shop_is_environment_not_a_replacement(conn, clock):
    add_case(conn, clock, 1, 10); add_case(conn, clock, 2, 1)
    conn.execute("UPDATE devices SET signal_pct=20 WHERE merchant_id=?", (MID,)); conn.commit()
    RecurrenceJob(conn, clock).run([MID])
    t = conn.execute("SELECT cause, action FROM root_cause_tasks").fetchone()
    assert t["cause"] == "ENVIRONMENT" and "network" in t["action"]


def test_repeated_how_to_is_merchant_needs_help(conn, clock):
    add_case(conn, clock, 1, 10, "C9_HOWTO_QR"); add_case(conn, clock, 2, 1, "C9_HOWTO_QR")
    RecurrenceJob(conn, clock).run([MID])
    assert conn.execute("SELECT cause FROM root_cause_tasks").fetchone()[0] == "MERCHANT_NEEDS_HELP"


def test_same_bank_across_many_merchants_is_paytm_side_and_an_incident_candidate(conn, clock):
    others = [r[0] for r in conn.execute("SELECT merchant_id FROM merchants WHERE merchant_id != ? ORDER BY merchant_id LIMIT 10", (MID,))]
    for m in others + [MID]:
        conn.execute("UPDATE bank SET ifsc='HDFC0001234' WHERE merchant_id=?", (m,))
    for m in others:
        add_case(conn, clock, 1, 3, "C4_SETTLEMENT", mid=m)
    add_case(conn, clock, 1, 10, "C4_SETTLEMENT"); add_case(conn, clock, 2, 1, "C4_SETTLEMENT")
    RecurrenceJob(conn, clock).run([MID])
    assert conn.execute("SELECT cause FROM root_cause_tasks WHERE merchant_id=?", (MID,)).fetchone()[0] == "PAYTM_SIDE"
    ic = conn.execute("SELECT kind, member_count FROM incident_candidates").fetchone()
    assert (ic["kind"], ic["member_count"]) == ("REPEAT_FLEET", 11)


# ---------------------------------------------------------------- fleet grouping (pincode / bank, never city)
def pincode_merchants(conn, pin, n):
    return [r[0] for r in conn.execute("SELECT merchant_id FROM merchants WHERE pincode=? ORDER BY merchant_id LIMIT ?", (pin, n))]


def hb_lost(conn, clock, mids, minutes_apart, start_min=60, prefix="e"):
    for i, m in enumerate(mids):
        ts = iso(clock.now() - timedelta(minutes=start_min) + timedelta(minutes=i * minutes_apart))
        append_event(conn, event_id=f"{prefix}-{m}-{i}", type="DEVICE_HEARTBEAT_LOST", payload={"device_id": f"DEV-{m}"}, ts_sim=ts, merchant_id=m)


def test_ten_devices_one_pincode_within_ten_minutes_is_an_incident_candidate(conn, clock):
    mids = pincode_merchants(conn, "560038", 10)
    assert len(mids) == 10
    hb_lost(conn, clock, mids, minutes_apart=1)                 # spread over 9 minutes
    ids = RecurrenceJob(conn, clock).detect_incident_candidates()
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM incident_candidates").fetchone()
    assert (row["kind"], row["area"], row["member_count"]) == ("HEARTBEAT_PINCODE", "560038", 10)


def test_grouping_by_city_would_be_wrong_ten_devices_over_three_pincodes_is_no_candidate(conn, clock):
    """The named Watch out: 10 devices in ONE CITY (Bengaluru) but 3-4 per pincode in the same 10 minutes."""
    mids = pincode_merchants(conn, "560038", 4) + pincode_merchants(conn, "560068", 3) + pincode_merchants(conn, "560001", 3)
    assert len(mids) == 10
    hb_lost(conn, clock, mids, minutes_apart=1)
    assert RecurrenceJob(conn, clock).detect_incident_candidates() == []
    assert conn.execute("SELECT COUNT(*) FROM incident_candidates").fetchone()[0] == 0


def test_window_is_ten_minutes_and_ten_is_the_minimum(conn, clock):
    mids = pincode_merchants(conn, "560038", 10)
    hb_lost(conn, clock, mids, minutes_apart=2)                 # 18 minutes end to end: no ten fit inside 10 minutes
    assert RecurrenceJob(conn, clock).detect_incident_candidates() == []
    conn2_mids = pincode_merchants(conn, "580029", 9)
    hb_lost(conn, clock, conn2_mids, minutes_apart=1, prefix="n")
    assert RecurrenceJob(conn, clock).detect_incident_candidates() == []            # nine is not ten


def test_ten_settlements_failing_on_one_bank_within_thirty_minutes(conn, clock):
    mids = [r[0] for r in conn.execute("SELECT merchant_id FROM merchants ORDER BY merchant_id LIMIT 10")]
    for i, m in enumerate(mids):
        conn.execute("UPDATE bank SET ifsc='SBIN0000001' WHERE merchant_id=?", (m,))
        append_event(conn, event_id=f"sf-{m}", type="SETTLEMENT_FAILED", payload={}, merchant_id=m,
                     ts_sim=iso(clock.now() - timedelta(minutes=50) + timedelta(minutes=3 * i)))      # 27 minutes end to end
    conn.commit()
    ids = RecurrenceJob(conn, clock).detect_incident_candidates()
    row = conn.execute("SELECT * FROM incident_candidates").fetchone()
    assert len(ids) == 1 and (row["kind"], row["area"], row["member_count"]) == ("SETTLEMENT_BANK", "SBIN", 10)


def test_settlements_split_across_two_banks_are_not_an_incident(conn, clock):
    mids = [r[0] for r in conn.execute("SELECT merchant_id FROM merchants ORDER BY merchant_id LIMIT 10")]
    for i, m in enumerate(mids):
        conn.execute("UPDATE bank SET ifsc=? WHERE merchant_id=?", ("SBIN0000001" if i % 2 else "HDFC0000001", m))
        append_event(conn, event_id=f"sf-{m}", type="SETTLEMENT_FAILED", payload={}, merchant_id=m, ts_sim=iso(clock.now() - timedelta(minutes=40 - i)))
    conn.commit()
    assert RecurrenceJob(conn, clock).detect_incident_candidates() == []


def test_detection_is_idempotent(conn, clock):
    hb_lost(conn, clock, pincode_merchants(conn, "560038", 10), minutes_apart=1)
    job = RecurrenceJob(conn, clock)
    job.detect_incident_candidates(); job.detect_incident_candidates()
    assert conn.execute("SELECT COUNT(*) FROM incident_candidates").fetchone()[0] == 1


def test_device_batch_failing_across_merchants_is_reported_to_product_quality(conn, clock):
    devs = [r[0] for r in conn.execute("SELECT device_id FROM devices WHERE model='Soundbox 4G' ORDER BY device_id LIMIT 6")]
    assert len(devs) == 6
    for i, d in enumerate(devs):
        conn.execute("UPDATE devices SET installed_at='2026-03-10T09:00:00+05:30' WHERE device_id=?", (d,))
        m = conn.execute("SELECT merchant_id FROM devices WHERE device_id=?", (d,)).fetchone()[0]
        if i < 5:
            conn.execute("INSERT INTO device_replacements VALUES (?,?,?,?,?,?,?)", (f"R-B{i}", d, m, "NO_POWER", "DELIVERED", iso(clock.now() - timedelta(days=5 + i)), iso(clock.now())))
    conn.commit()
    ids = RecurrenceJob(conn, clock).report_device_batches()
    row = conn.execute("SELECT * FROM fleet_reports").fetchone()
    assert len(ids) == 1 and row["batch"] == "Soundbox 4G / 2026-03" and row["member_count"] == 5 and row["route_to"] == "product_quality"
    assert RecurrenceJob(conn, clock).report_device_batches(min_devices=6) == []


def test_job_never_contacts_or_acts(conn, clock):
    add_case(conn, clock, 1, 10); add_case(conn, clock, 2, 1)
    tables = ("comms_outbox", "actions", "ledger", "tickets", "bookings", "device_replacements")
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    RecurrenceJob(conn, clock).run([MID])
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables] == before
