from pathlib import Path

import pytest

from core.engine.playbook import load_playbooks
from core.engine.triage import EventTriage, OpenCase, triage_event
from packs.paytm_merchant.actions import build_registry

PACK = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant"


@pytest.fixture(scope="module")
def pbs():
    return load_playbooks(PACK / "playbooks", build_registry())


def ev(etype, mid="M-0005", eid="e1", **kw):
    return {"event_id": eid, "type": etype, "merchant_id": mid, **kw}


def test_event_maps_to_playbook_via_triggers(pbs):
    assert triage_event(ev("DEVICE_HEARTBEAT_LOST"), pbs, []) == EventTriage("C2_SOUNDBOX_FAULT", None)
    assert triage_event(ev("SIGNUP_STALLED", "M-0006"), pbs, []) == EventTriage("S1_ACTIVATION", None)
    assert triage_event(ev("KYC_REJECTED", "M-0006"), pbs, []).playbook_id == "S1_ACTIVATION"


def test_event_with_no_trigger_maps_to_nothing(pbs):
    assert triage_event(ev("USAGE_DROP"), pbs, []) == EventTriage(None, None)
    assert triage_event(ev("PAYMENT_SUCCESS"), pbs, []) == EventTriage(None, None)


def test_two_heartbeat_lost_events_for_one_merchant_make_one_case(pbs):
    """The ticket test: the caller opens a case for the first event; the second links to it."""
    open_cases: list[OpenCase] = []
    cases_opened = 0
    for eid in ("e1", "e2"):
        t = triage_event(ev("DEVICE_HEARTBEAT_LOST", eid=eid), pbs, open_cases)
        if t.link_case_id is None:
            cases_opened += 1
            open_cases.append(OpenCase(f"C-{cases_opened}", "M-0005", t.playbook_id, "ACTIVE"))
        else:
            assert t.link_case_id == "C-1"
    assert cases_opened == 1


def test_links_even_when_the_open_case_is_waiting_or_escalated(pbs):
    for state in ("NEW", "ACTIVE", "WAITING", "PARKED", "NOT_NOW", "ESCALATED", "RETURNED_TO_AGENT", "BLOCKED"):
        t = triage_event(ev("DEVICE_HEARTBEAT_LOST"), pbs, [OpenCase("C-9", "M-0005", "C2_SOUNDBOX_FAULT", state)])
        assert t.link_case_id == "C-9", state


def test_a_resolved_or_declined_case_is_not_linked_so_a_new_one_opens(pbs):
    for state in ("RESOLVED", "DECLINED"):
        t = triage_event(ev("DEVICE_HEARTBEAT_LOST"), pbs, [OpenCase("C-9", "M-0005", "C2_SOUNDBOX_FAULT", state)])
        assert t == EventTriage("C2_SOUNDBOX_FAULT", None), state


def test_other_merchants_and_other_playbooks_do_not_link(pbs):
    cases = [OpenCase("C-1", "M-0099", "C2_SOUNDBOX_FAULT", "ACTIVE"), OpenCase("C-2", "M-0005", "S1_ACTIVATION", "ACTIVE")]
    assert triage_event(ev("DEVICE_HEARTBEAT_LOST"), pbs, cases) == EventTriage("C2_SOUNDBOX_FAULT", None)


def test_event_carrying_an_open_case_id_goes_to_that_case(pbs):
    cases = [OpenCase("C-7", "M-0006", "S1_ACTIVATION", "WAITING")]
    t = triage_event({"event_id": "t1", "type": "TIMER_FIRED", "merchant_id": "M-0006", "case_id": "C-7"}, pbs, cases)
    assert t == EventTriage("S1_ACTIVATION", "C-7")
    closed = [OpenCase("C-7", "M-0006", "S1_ACTIVATION", "RESOLVED")]
    assert triage_event({"event_id": "t2", "type": "TIMER_FIRED", "merchant_id": "M-0006", "case_id": "C-7"}, pbs, closed) == EventTriage(None, None)


def test_several_open_cases_link_deterministically_to_the_lowest_id(pbs):
    cases = [OpenCase("C-5", "M-0005", "C2_SOUNDBOX_FAULT", "ACTIVE"), OpenCase("C-2", "M-0005", "C2_SOUNDBOX_FAULT", "ACTIVE")]
    assert triage_event(ev("DEVICE_HEARTBEAT_LOST"), pbs, cases).link_case_id == "C-2"


def test_triage_event_is_pure(pbs):
    cases = [OpenCase("C-1", "M-0005", "C2_SOUNDBOX_FAULT", "ACTIVE")]
    snapshot = list(cases)
    e = ev("DEVICE_HEARTBEAT_LOST")
    first = triage_event(e, pbs, cases)
    assert triage_event(e, pbs, cases) == first and cases == snapshot and e == ev("DEVICE_HEARTBEAT_LOST")
