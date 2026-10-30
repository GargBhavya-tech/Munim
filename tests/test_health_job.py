"""#94 merchant health and predictive faults: three injected scenarios on the seeded data, plus the guards behind them."""
import json
import statistics
from datetime import date, timedelta

import pytest

from jobs.health import HealthJob, record_telemetry

TODAY = date(2026, 10, 31)
LAST3 = [(TODAY - timedelta(days=i)).isoformat() for i in (1, 2, 3)]       # Oct 30, 29, 28 (completed days)


def baseline(conn, mid, day_iso):
    d = date.fromisoformat(day_iso)
    vals = []
    for k in (1, 2, 3, 4):
        b = (d - timedelta(days=7 * k)).isoformat()
        if b < "2026-10-01":
            continue
        vals.append(conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE merchant_id=? AND status='SUCCESS' AND substr(ts,1,10)=?", (mid, b)).fetchone()[0])
    return statistics.median(vals)


def set_day(conn, mid, day_iso, paise):
    """Replace a day's successful payments with one payment of exactly ``paise`` (0 = no payments)."""
    conn.execute("DELETE FROM payments WHERE merchant_id=? AND substr(ts,1,10)=?", (mid, day_iso))
    if paise:
        conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,NULL)", (f"P-T-{mid}-{day_iso}", mid, int(paise), "SUCCESS", f"{day_iso}T12:00:00+05:30", f"REF{mid}{day_iso}"))


def drop(conn, mid, factors, days=LAST3):
    # compute every baseline before editing: an edited day is another day's baseline only a week later, but be safe
    bases = {d: baseline(conn, mid, d) for d in days}
    for d, f in zip(days, factors):
        set_day(conn, mid, d, bases[d] * f)
    conn.commit()


def events(conn, mid, typ="USAGE_DROP"):
    return [(json.loads(r["payload_json"])) for r in conn.execute("SELECT payload_json FROM events WHERE merchant_id=? AND type=? ORDER BY seq", (mid, typ))]


def city_mids(conn, city):
    return [r[0] for r in conn.execute("SELECT merchant_id FROM merchants WHERE city=? ORDER BY merchant_id", (city,))]


# the merchants used below are plain seeded merchants (not one of the scripted M-0001..M-0012 situations)
TARGET = "M-0115"     # Bengaluru 560068, has a Soundbox (D-0056)
CONTROL = "M-0150"    # same pincode, has a Soundbox, left alone


def test_control_merchants_are_quiet_on_untouched_seed(conn, clock):
    r = HealthJob(conn, clock).run([TARGET, CONTROL, "M-0011"])
    assert [f for f in r.findings if f.event_type] == []
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='USAGE_DROP'").fetchone()[0] == 0


def test_scenario_1_device_offline_gives_a_service_ticket_event(conn, clock):
    hb = (clock.now() - timedelta(hours=8)).isoformat()
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=? WHERE merchant_id=?", (hb, TARGET))
    conn.execute("DELETE FROM payments WHERE merchant_id=? AND ts > ?", (TARGET, hb))
    conn.commit()
    r = HealthJob(conn, clock).run([TARGET, CONTROL])
    f = r.for_merchant(TARGET)[0]
    assert (f.diagnosis, f.action, f.event_type) == ("DEVICE_OFFLINE", "SERVICE_TICKET", "USAGE_DROP")
    assert r.for_merchant(CONTROL) == []
    ev = events(conn, TARGET)
    assert len(ev) == 1 and ev[0]["diagnosis"] == "DEVICE_OFFLINE" and ev[0]["action"] == "SERVICE_TICKET"


def test_offline_but_still_selling_is_not_a_service_ticket(conn, clock):
    hb = (clock.now() - timedelta(hours=8)).isoformat()
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=? WHERE merchant_id=?", (hb, TARGET))
    conn.execute("DELETE FROM payments WHERE merchant_id=? AND ts > ?", (TARGET, hb))
    conn.execute("INSERT INTO payments VALUES ('P-T-SOLD',?,50000,'SUCCESS',?,'REFSOLD',NULL)", (TARGET, (clock.now() - timedelta(hours=2)).isoformat()))   # still selling
    conn.commit()
    assert HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET) == []


def test_pincode_wide_outage_is_not_a_pile_of_service_tickets(conn, clock):
    pin = conn.execute("SELECT pincode FROM merchants WHERE merchant_id=?", (TARGET,)).fetchone()[0]
    hb = (clock.now() - timedelta(hours=8)).isoformat()
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=? WHERE merchant_id IN (SELECT merchant_id FROM merchants WHERE pincode=?)", (hb, pin))
    conn.execute("DELETE FROM payments WHERE ts > ? AND merchant_id IN (SELECT merchant_id FROM merchants WHERE pincode=?)", (hb, pin))
    conn.commit()
    r = HealthJob(conn, clock).run([TARGET, CONTROL])
    assert [f.diagnosis for f in r.findings] == ["AREA_OUTAGE", "AREA_OUTAGE"]
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='USAGE_DROP'").fetchone()[0] == 0


def test_scenario_2_gradual_competitor_switch_gives_retention_handoff(conn, clock):
    drop(conn, TARGET, [0.65, 0.55, 0.45])         # oldest -> newest day order matches LAST3 = Oct 30, 29, 28; all below 70%
    r = HealthJob(conn, clock, competitor_signals={TARGET}).run([TARGET, CONTROL])
    f = r.for_merchant(TARGET)[0]
    assert (f.diagnosis, f.action, f.event_type) == ("COMPETITOR_SWITCH", "RETENTION_HANDOFF", "USAGE_DROP")
    assert f.detail["persistent_days"] == 3 and all(x < 0.7 for x in f.detail["ratios"])
    assert r.for_merchant(CONTROL) == []
    assert events(conn, TARGET)[0]["action"] == "RETENTION_HANDOFF"


def test_competitor_signal_can_come_from_a_profile_fact(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    conn.execute("INSERT INTO profile_facts(merchant_id, key, value, source, valid_from) VALUES (?,?,?,?,?)",
                 (TARGET, "competitor_qr_seen", "true", "field_photo", "2026-10-29T10:00:00+05:30"))
    conn.commit()
    assert HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0].action == "RETENTION_HANDOFF"


def test_drop_without_competitor_signal_is_not_a_retention_handoff(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    f = HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0]
    assert (f.diagnosis, f.action) == ("UNEXPLAINED_DROP", "INSIGHTS_OFFER")


def test_scenario_3_citywide_rain_dip_produces_no_action(conn, clock):
    """The Watch out: alerting on a citywide dip. Every Bengaluru merchant drops to half for three days."""
    mids = city_mids(conn, "Bengaluru")
    assert len(mids) > 50
    for m in mids:
        drop(conn, m, [0.5, 0.5, 0.5])
    r = HealthJob(conn, clock, competitor_signals={TARGET}).run(mids)      # even a competitor signal must not turn rain into a handoff
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='USAGE_DROP'").fetchone()[0] == 0
    assert [f for f in r.findings if f.event_type] == []
    assert r.for_merchant(TARGET)[0].diagnosis == "AREA_WIDE_DIP" and r.for_merchant(TARGET)[0].action == "NO_ACTION"
    assert sum(1 for f in r.findings if f.diagnosis == "AREA_WIDE_DIP") >= len(mids) - 6      # allow a few device-offline seeds


def test_same_drop_on_one_merchant_only_does_alert(conn, clock):
    """The control for the rain test: the identical numbers on ONE merchant (neighbours normal) are a real drop."""
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    f = HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0]
    assert f.event_type == "USAGE_DROP" and f.diagnosis != "AREA_WIDE_DIP"


def test_a_merchant_much_worse_than_a_mild_area_dip_still_alerts(conn, clock):
    pin = conn.execute("SELECT pincode FROM merchants WHERE merchant_id=?", (TARGET,)).fetchone()[0]
    for m in [r[0] for r in conn.execute("SELECT merchant_id FROM merchants WHERE pincode=? AND merchant_id != ?", (pin, TARGET))]:
        drop(conn, m, [0.75, 0.75, 0.75])
    drop(conn, TARGET, [0.2, 0.2, 0.2])
    assert HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0].event_type == "USAGE_DROP"


def test_persistence_needs_three_days_below_threshold(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 1.0])
    assert HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET) == []


def test_festival_and_month_end_adjustment_changes_the_expectation(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    adj = {d: 0.5 for d in LAST3}                # the caller says these three days were expected to be half of normal (bandh/holiday)
    assert HealthJob(conn, clock, adjustments=adj).run([TARGET]).for_merchant(TARGET) == []
    assert HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0].event_type == "USAGE_DROP"


def test_festival_baseline_days_are_normalised_out(conn, clock):
    """The comparison weekdays were festival days (x2). Without the flag a flat week looks like a 50% drop; with it, it does not."""
    drop(conn, TARGET, [1.0, 1.0, 1.0])
    inflated = {}
    for d in LAST3:
        for k in (1, 2, 3, 4):
            b = (date.fromisoformat(d) - timedelta(days=7 * k)).isoformat()
            if b >= "2026-10-01":
                inflated[b] = 2.0
    for d in inflated:
        conn.execute("UPDATE payments SET amount_paise = amount_paise * 2 WHERE merchant_id=? AND substr(ts,1,10)=?", (TARGET, d))
    conn.commit()
    assert HealthJob(conn, clock, adjustments=inflated).run([TARGET]).for_merchant(TARGET) == []
    assert HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0].event_type == "USAGE_DROP"      # the false alarm the flag prevents


def test_settlement_failure_with_a_drop_is_a_support_handoff(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    conn.execute("INSERT INTO settlements VALUES ('S-T-1',?,500000,'FAILED','2026-10-29T10:00:00+05:30',NULL,'BANK_REJECTED')", (TARGET,))
    conn.commit()
    f = HealthJob(conn, clock, competitor_signals={TARGET}).run([TARGET]).for_merchant(TARGET)[0]
    assert (f.diagnosis, f.action) == ("SETTLEMENT_FAILURE", "SUPPORT_HANDOFF")


def test_seven_days_of_zero_gives_a_check_in(conn, clock):
    for i in range(1, 9):
        set_day(conn, TARGET, (TODAY - timedelta(days=i)).isoformat(), 0)
    conn.commit()
    f = HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0]
    assert (f.diagnosis, f.action) == ("ZERO_ACTIVITY", "CHECK_IN_CALL") and f.detail["zero_days"] >= 7


def test_six_days_of_zero_is_not_yet_a_check_in(conn, clock):
    for i in range(1, 7):
        set_day(conn, TARGET, (TODAY - timedelta(days=i)).isoformat(), 0)
    conn.commit()
    f = HealthJob(conn, clock).run([TARGET]).for_merchant(TARGET)[0]
    assert f.diagnosis != "ZERO_ACTIVITY"          # it is a persistent drop to zero, diagnosed as such


def test_rerun_does_not_duplicate_events(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    HealthJob(conn, clock).run([TARGET]); HealthJob(conn, clock).run([TARGET])
    assert len(events(conn, TARGET)) == 1


def test_predictive_fault_emits_device_degrading_before_failure(conn, clock):
    dev = conn.execute("SELECT device_id FROM devices WHERE merchant_id=?", (TARGET,)).fetchone()[0]
    start = clock.now() - timedelta(days=6)
    for i, (b, s) in enumerate([(82, 88), (70, 80), (55, 70), (42, 55), (28, 40), (18, 30)]):
        record_telemetry(conn, dev, (start + timedelta(days=i)).isoformat(), battery_pct=b, signal_pct=s, heartbeat_gap_s=60)
    r = HealthJob(conn, clock).run([TARGET])
    f = [x for x in r.for_merchant(TARGET) if x.event_type == "DEVICE_DEGRADING"][0]
    assert set(f.detail["signals"]) == {"BATTERY_FALLING", "SIGNAL_FALLING"} and f.detail["device_id"] == dev
    assert events(conn, TARGET, "DEVICE_DEGRADING")[0]["device_id"] == dev


def test_heartbeat_gaps_growing_and_steady_devices(conn, clock):
    dev = conn.execute("SELECT device_id FROM devices WHERE merchant_id=?", (TARGET,)).fetchone()[0]
    steady = conn.execute("SELECT device_id FROM devices WHERE merchant_id=?", (CONTROL,)).fetchone()[0]
    start = clock.now() - timedelta(days=6)
    for i, g in enumerate([60, 60, 70, 300, 900, 1500]):
        record_telemetry(conn, dev, (start + timedelta(days=i)).isoformat(), battery_pct=90, signal_pct=80, heartbeat_gap_s=g)
        record_telemetry(conn, steady, (start + timedelta(days=i)).isoformat(), battery_pct=90 - i, signal_pct=80, heartbeat_gap_s=60)
    r = HealthJob(conn, clock).run([TARGET, CONTROL])
    assert [x.detail["signals"] for x in r.for_merchant(TARGET)] == [["HEARTBEAT_GAPS_GROWING"]]
    assert r.for_merchant(CONTROL) == []


def test_degrading_is_not_raised_for_a_device_that_already_failed(conn, clock):
    dev = conn.execute("SELECT device_id FROM devices WHERE merchant_id=?", (TARGET,)).fetchone()[0]
    start = clock.now() - timedelta(days=6)
    for i, b in enumerate([80, 60, 40, 25, 15, 8]):
        record_telemetry(conn, dev, (start + timedelta(days=i)).isoformat(), battery_pct=b, signal_pct=80, heartbeat_gap_s=60)
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=? WHERE device_id=?", (clock.now_iso(), dev))
    conn.commit()
    assert events(conn, TARGET, "DEVICE_DEGRADING") == [] and HealthJob(conn, clock).run([TARGET]) and events(conn, TARGET, "DEVICE_DEGRADING") == []


def test_health_score_components_and_retention_queue(conn, clock):
    job = HealthJob(conn, clock, sentiments={TARGET: 1.0})
    r = job.run([CONTROL, TARGET])
    comps = json.loads(conn.execute("SELECT components_json FROM merchant_health WHERE merchant_id=?", (TARGET,)).fetchone()[0])
    assert {k: comps[k] for k in ("device", "tickets", "sentiment", "contract")} == {"device": 20, "tickets": 20, "sentiment": 10, "contract": 10}
    assert r.scores[TARGET] == sum(comps.values())
    assert r.retention == [] or all(m in (CONTROL, TARGET) for m in r.retention)
    # bad: offline device, three open tickets, a long drop, unhappy
    hb = (clock.now() - timedelta(hours=8)).isoformat()
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=? WHERE merchant_id=?", (hb, CONTROL))
    for i in range(3):
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,NULL)", (f"T-H{i}", CONTROL, "DEVICE", "OPEN", f"D-{i}", "x", "2026-10-30T10:00:00+05:30"))
    drop(conn, CONTROL, [0.1, 0.1, 0.1])
    r2 = HealthJob(conn, clock, sentiments={CONTROL: -1.0}).run([CONTROL])
    c2 = json.loads(conn.execute("SELECT components_json FROM merchant_health WHERE merchant_id=?", (CONTROL,)).fetchone()[0])
    assert (c2["device"], c2["tickets"], c2["sentiment"]) == (0, 5, 0) and c2["usage"] <= 6
    assert r2.scores[CONTROL] < 50 and r2.retention == [CONTROL]
    assert conn.execute("SELECT merchant_id FROM retention_queue").fetchall()[0][0] == CONTROL


def test_job_never_contacts_or_acts(conn, clock):
    drop(conn, TARGET, [0.5, 0.5, 0.5])
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("comms_outbox", "actions", "ledger", "tickets", "bookings")]
    HealthJob(conn, clock, competitor_signals={TARGET}).run([TARGET])
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("comms_outbox", "actions", "ledger", "tickets", "bookings")] == before
