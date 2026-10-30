"""#88 I1 area incidents: 30 devices in one pincode within 6 minutes -> ONE incident, 0 replacements, one message per merchant; the
impact graph comes from database rows only; grouping is by pincode, never by city; resolving releases the holds."""
import json
from datetime import timedelta

import pytest

from core.actions.types import ActionIntent
from core.engine.scripted import scripted_decider
from jobs.incidents import IncidentManager, on_hold
from tests.test_scenarios_s1_c2 import SCRIPT
from tests.world import World

PIN = "560034"


def area_world(db_path, clock, n=30, pincode=PIN, city_only=False):
    """n seeded merchants with a device moved into one pincode (or, for the city test, one city but n different pincodes)."""
    w = World(db_path, clock, None)
    w.engine.decider = scripted_decider(SCRIPT)
    mids = [r[0] for r in w.conn.execute("SELECT m.merchant_id FROM merchants m JOIN devices d ON d.merchant_id = m.merchant_id "
                                         "WHERE d.status = 'ONLINE' AND NOT EXISTS (SELECT 1 FROM device_replacements r WHERE r.merchant_id = m.merchant_id) "
                                         "ORDER BY m.merchant_id LIMIT ?", (n,))]
    assert len(mids) == n
    for i, mid in enumerate(mids):
        pin = f"5600{i:02d}" if city_only else pincode
        w.conn.execute("UPDATE merchants SET pincode = ?, city = 'Bengaluru' WHERE merchant_id = ?", (pin, mid))
        w.conn.execute("INSERT OR REPLACE INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'whatsapp', 'GRANTED', 'test', ?)", (mid, clock.now_iso()))
    return w, mids


def go_dark(w, mids, minutes=6, manager_first=None):
    """The devices drop off. With ``manager_first`` the incident job runs on the event log BEFORE the cases are dispatched,
    the way the production loop runs it (jobs first, then the dispatcher)."""
    step = timedelta(minutes=minutes) / len(mids)
    for i, mid in enumerate(mids):
        w.control(f"/mock/control/device/{mid}", status="OFFLINE")
        dev = w.conn.execute("SELECT device_id FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0]
        w.inject(f"HB-{i}", "DEVICE_HEARTBEAT_LOST", mid=mid, payload={"device_id": dev})
        w.clock.advance(step)
    res = manager_first.run() if manager_first is not None else None
    w.dispatcher.run()
    return res


def manager(w):
    m = IncidentManager(w.conn, w.clock, w.ledger, w.executor, w.engine.case_manager, w.engine.repo)
    w.engine.hold_fn = m.hold_note
    return m


def new_replacements(w):
    return w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE status != 'CANCELLED' AND created_at >= '2026-10-31'").fetchone()[0]


def test_thirty_devices_in_one_pincode_within_six_minutes_is_one_incident_with_no_replacements(db_path, clock):
    w, mids = area_world(db_path, clock)
    res = go_dark(w, mids, manager_first=manager(w))
    assert len(res.incidents) == 1 and res.newly_held == 30
    inc = w.conn.execute("SELECT * FROM incidents").fetchall()
    assert len(inc) == 1 and (inc[0]["kind"], inc[0]["area"], inc[0]["status"]) == ("HEARTBEAT_PINCODE", PIN, "OPEN")
    # the merchants answer their checklists; the hold still stops every replacement
    for i, mid in enumerate(mids):
        w.inject(f"R-{i}", "MERCHANT_MESSAGE", mid=mid, payload={"text": "abhi bhi band hai"})
    w.dispatcher.run()
    assert new_replacements(w) == 0
    parked = w.conn.execute("SELECT COUNT(*) FROM cases WHERE playbook_id = 'C2_SOUNDBOX_FAULT' AND state = 'PARKED'").fetchone()[0]
    assert parked == 30
    sent = [json.loads(r[0]) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'MESSAGE_SENT'")]
    assert sent == []                                       # the C2 cases parked before any checklist went out
    notices = {r["merchant_id"]: (r["status"], r["action_status"]) for r in w.conn.execute("SELECT * FROM incident_notices")}
    held_back = {mid for mid, (st, _) in notices.items() if st != "SENT"}
    assert len(notices) == 30 and held_back == {"M-0003"}             # M-0003 set a contact window; 09:xx is outside it
    assert notices["M-0003"] == ("NOT_SENT", "REFUSED")
    assert res.notices_sent == 29                           # the incident notice is each merchant's ONE message


def test_one_message_per_merchant_even_when_the_job_runs_again(db_path, clock):
    w, mids = area_world(db_path, clock, n=12)
    m = manager(w)
    first = go_dark(w, mids, minutes=4, manager_first=m)
    again = m.run()
    assert again.incidents == first.incidents and again.notices_sent == 0
    iid = first.incidents[0]
    rows = w.conn.execute("SELECT merchant_id, status FROM incident_notices WHERE incident_id = ?", (iid,)).fetchall()
    assert len(rows) == 12 and {r["merchant_id"] for r in rows} == set(mids)
    texts = [json.loads(r[0]) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'ACTION_RESULT' AND case_id = ?", (iid,))]
    assert sum(1 for t in texts if t.get("action") == "send_message" and t.get("status") == "OK") == first.notices_sent


def test_nine_devices_or_a_city_spread_is_not_an_incident(db_path, clock):
    w, mids = area_world(db_path, clock, n=9)
    go_dark(w, mids)
    assert manager(w).run().incidents == []


def test_grouping_is_by_pincode_never_by_city(db_path, clock):
    w, mids = area_world(db_path, clock, n=15, city_only=True)       # 15 devices, one city, 15 pincodes
    go_dark(w, mids)
    assert manager(w).run().incidents == []


def test_the_impact_graph_is_built_from_rows_and_resolving_releases_the_holds(db_path, clock):
    w, mids = area_world(db_path, clock, n=10)
    m = manager(w)
    iid = go_dark(w, mids, minutes=3, manager_first=m).incidents[0]
    g = m.impact_graph(iid)
    area = f"area:{PIN}"
    assert g.nodes[area]["kind"] == "pincode" and len(list(g.successors(area))) == 10
    for mid in mids:
        dev = w.conn.execute("SELECT device_id FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0]
        assert g.has_edge(area, f"device:{dev}") and g.has_edge(f"device:{dev}", f"merchant:{mid}")
        assert any(g.nodes[s]["playbook"] == "C2_SOUNDBOX_FAULT" for s in g.successors(f"merchant:{mid}"))
    assert on_hold(w.conn, mids[0]) == iid
    released = m.resolve(iid)
    assert on_hold(w.conn, mids[0]) is None and len(released) == 10
    out = w.executor.submit(ActionIntent("create_device_replacement", {"merchant_id": mids[0], "reason": "NO_ANNOUNCEMENT"}, case_id="CS-X", step_seq=1,
                                         allowlist=("create_device_replacement",)))
    assert out.status == "OK"                                         # after the incident a per-merchant fix is allowed again


def test_a_human_can_still_act_during_an_incident(db_path, clock):
    from core.actions.types import Principal
    w, mids = area_world(db_path, clock, n=10)
    go_dark(w, mids, minutes=3, manager_first=manager(w))
    agent = w.executor.submit(ActionIntent("create_device_replacement", {"merchant_id": mids[0], "reason": "NO_ANNOUNCEMENT"}, case_id="CS-X", step_seq=1,
                                           allowlist=("create_device_replacement",)))
    assert agent.status == "REFUSED" and [v.rule_id for v in agent.violations] == ["INCIDENT-HOLD-001"]
    human = w.executor.submit(ActionIntent("create_device_replacement", {"merchant_id": mids[0], "reason": "NO_ANNOUNCEMENT"}, case_id="CS-X", step_seq=2,
                                           principal=Principal("HUMAN", "u-1", "technician")))
    assert human.status == "OK", human.error


def test_a_device_that_goes_dark_after_the_first_run_inside_the_same_window_joins_the_incident(db_path, clock):
    """Late joiner: the candidate is keyed by its window start, so the old INSERT OR IGNORE kept the 10-member row and dropped the 11th and 12th."""
    w, mids = area_world(db_path, clock, n=12)
    m = manager(w)
    for i, mid in enumerate(mids[:10]):
        w.control(f"/mock/control/device/{mid}", status="OFFLINE")
        dev = w.conn.execute("SELECT device_id FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0]
        w.inject(f"HB-{i}", "DEVICE_HEARTBEAT_LOST", mid=mid, payload={"device_id": dev})
        w.clock.advance(timedelta(seconds=30))
    first = m.run()
    assert len(first.incidents) == 1 and first.newly_held == 10
    for i, mid in enumerate(mids[10:], start=10):            # two more, still inside the 10-minute window of the first loss
        w.control(f"/mock/control/device/{mid}", status="OFFLINE")
        dev = w.conn.execute("SELECT device_id FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0]
        w.inject(f"HB-{i}", "DEVICE_HEARTBEAT_LOST", mid=mid, payload={"device_id": dev})
        w.clock.advance(timedelta(seconds=30))
    second = m.run()
    assert second.incidents == first.incidents and second.newly_held == 2           # the SAME incident, two more members
    held = {r[0] for r in w.conn.execute("SELECT merchant_id FROM incident_members WHERE incident_id = ?", (first.incidents[0],))}
    assert held == set(mids)
    assert all(on_hold(w.conn, mid) == first.incidents[0] for mid in mids)
    assert w.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
