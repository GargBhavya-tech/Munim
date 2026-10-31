import json
from datetime import timedelta

import pytest

from core.engine.events import append_event
from core.memory.profile import ProfileStore
from core.memory.timeline import Timeline

A, B = "M-0001", "M-0002"


def mk_case(conn, cid, mid, opened, state="ACTIVE"):
    conn.execute("INSERT INTO cases(case_id,merchant_id,playbook_id,state,lane,opened_at) VALUES (?,?,?,?,?,?)", (cid, mid, "S1", state, "service", opened))


def mk_event(conn, eid, mid, cid, typ, ts, payload=None, link=True, link_case=None):
    append_event(conn, event_id=eid, type=typ, payload=payload or {}, ts_sim=ts, merchant_id=mid, case_id=cid)
    if link:
        conn.execute("INSERT INTO case_events(case_id,event_id) VALUES (?,?)", (link_case or cid, eid))


@pytest.fixture
def world(conn, clock):
    mk_case(conn, "C-B1", B, "2026-10-02T09:00:00+05:30")
    mk_case(conn, "C-A2", A, "2026-10-20T09:00:00+05:30", "RESOLVED")
    mk_case(conn, "C-A1", A, "2026-10-05T09:00:00+05:30", "DECLINED")
    mk_event(conn, "EV-A1", A, "C-A1", "SIGNUP_STALLED", "2026-10-05T09:01:00+05:30")
    mk_event(conn, "EV-A2", A, "C-A2", "KYC_REJECTED", "2026-10-20T09:01:00+05:30", {"reason": "blurry"})
    mk_event(conn, "EV-B1", B, "C-B1", "DEVICE_DELIVERED", "2026-10-02T09:01:00+05:30", {"note": f"also saw {A}"})
    return conn


def test_cases_ordered_and_only_this_merchants(world, clock):
    t = Timeline(world, clock).build(A)
    assert [c["case_id"] for c in t["cases"]] == ["C-A1", "C-A2"]
    assert [e["type"] for c in t["cases"] for e in c["events"]] == ["SIGNUP_STALLED", "KYC_REJECTED"]


def test_context_for_A_has_no_B_identifiers(world, clock):
    # worst cases for a shared query: B's event wrongly linked to A's case, A's event payload quoting B's id
    mk_event(world, "EV-B2", B, "C-B1", "PAYMENT_FAILED", "2026-10-21T10:00:00+05:30", {"x": 1}, link_case="C-A2")
    mk_event(world, "EV-A3", A, "C-A2", "MERCHANT_MESSAGE", "2026-10-21T11:00:00+05:30", {"text": f"same as {B} shop"})
    ProfileStore(world, clock).set_fact(A, "note", f"neighbour {B}", "HUMAN_AGENT")
    tl = Timeline(world, clock)
    blob = tl.compact(A) + json.dumps(tl.build(A), ensure_ascii=False)
    for needle in (B, "C-B1", "EV-B1", "EV-B2", "DEVICE_DELIVERED", "PAYMENT_FAILED"):
        assert needle not in blob, needle
    assert "EV-A1" in blob and "[other]" in blob


def test_b_context_symmetrical(world, clock):
    blob = Timeline(world, clock).compact(B)
    assert "C-A1" not in blob and "C-A2" not in blob and "KYC_REJECTED" not in blob and "DEVICE_DELIVERED" in blob


def test_cross_case_facts_in_compact_form(world, clock):
    ps = ProfileStore(world, clock)
    ps.decline(A, "S3", clock.now() + timedelta(days=30))
    ps.set_do_not_contact(A)
    ps.set_fact(A, "language", "hi", "MERCHANT_SAID")
    ps.set_fact(A, "digital_comfort_voice", "high", "AGENT_INFERRED")
    c = Timeline(world, clock).compact(A)
    assert "DO_NOT_CONTACT" in c and "DECLINED(S3,until=2026-11-30)" in c
    assert "language=hi" in c and "digital_comfort_voice=high~inferred" in c


def test_compact_is_lean(world, clock):
    for i in range(30):
        mk_event(world, f"EV-L{i}", A, "C-A2", "PAYMENT_SUCCESS", f"2026-10-22T10:{i:02d}:00+05:30", {"big": "x" * 500})
    c = Timeline(world, clock).compact(A, max_events=6)
    assert c.count("PAYMENT_SUCCESS") == 6 and len(c) < 700 and "xxxx" not in c


def test_withdrawn_memory_consent_empties_timeline(world, clock):
    world.execute("INSERT OR REPLACE INTO consent(subject_id,channel,status,source,ts) VALUES (?,?,?,?,?)", (A, "memory", "REVOKED", "t", clock.now_iso()))
    t = Timeline(world, clock)
    assert t.build(A)["cases"] == [] and "withdrawn" in t.compact(A)
