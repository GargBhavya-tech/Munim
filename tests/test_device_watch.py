"""W1: a weakening Soundbox gets a heads-up before the merchant calls. Only a heads-up: it never repairs, replaces or visits."""
from pathlib import Path

import pytest

from core.engine.dispatcher import Dispatcher
from core.engine.playbook import load_playbooks
from packs.paytm_merchant.actions import build_registry
from tests.checkpoint_script import SCRIPT
from tests.world import World

PB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "playbooks"
MID = "M-0009"


@pytest.fixture
def playbook():
    return load_playbooks(PB_DIR, build_registry())["W1_DEVICE_WATCH"]


def test_the_watch_playbook_loads_and_is_opened_by_the_degrading_event_only(playbook):
    assert playbook.triggers.events == ["DEVICE_DEGRADING"] and playbook.triggers.intents == [] and playbook.half == "service"


def test_it_can_only_message_and_schedule_a_follow_up(playbook):
    assert {a.name for a in playbook.allowed_actions} == {"send_message", "schedule_follow_up"}
    for repair in ("restart_device", "push_device_config", "reset_device_volume", "create_device_replacement", "book_visit_or_call", "raise_ticket"):
        assert playbook.action(repair) is None


def test_it_ends_after_one_heads_up_or_when_the_device_goes_offline_and_the_fault_playbook_takes_over(playbook):
    assert playbook.max_touches == 1 and "device.status == 'OFFLINE'" in playbook.done_check and "touches >= 1" in playbook.done_check
    assert playbook.escalate_when == []


def test_the_fault_playbook_is_still_opened_by_a_lost_heartbeat_and_not_by_a_weakening_one():
    c2 = load_playbooks(PB_DIR, build_registry())["C2_SOUNDBOX_FAULT"]
    assert "DEVICE_HEARTBEAT_LOST" in c2.triggers.events and "DEVICE_DEGRADING" not in c2.triggers.events


class Recorder:
    """Replaces the engine's event handling so only the ROUTING is tested: which playbook a degrading device opens."""

    def __init__(self, w):
        self.w, self.seen = w, []
        w.engine.handle_event = lambda ev: self.seen.append((ev.type, ev.case_id)) or None
        self.dispatcher = Dispatcher(w.conn, w.engine, w.playbooks)

    def cases(self):
        return {c.playbook_id: c for c in self.w.engine.repo.cases_for(MID)}


@pytest.fixture
def rec(db_path, clock):
    return Recorder(World(db_path, clock, SCRIPT, mid=MID))


def test_a_degrading_device_opens_the_watch_case_for_that_merchant(rec):
    rec.w.inject("E-DEG-1", "DEVICE_DEGRADING", mid=MID)
    (d,) = rec.dispatcher.run()
    assert d.opened and list(rec.cases()) == ["W1_DEVICE_WATCH"]
    assert rec.seen == [("DEVICE_DEGRADING", rec.cases()["W1_DEVICE_WATCH"].case_id)]


def test_a_second_degrading_event_joins_the_open_watch_case_instead_of_opening_another(rec):
    rec.w.inject("E-DEG-1", "DEVICE_DEGRADING", mid=MID)
    rec.dispatcher.run()
    rec.w.inject("E-DEG-2", "DEVICE_DEGRADING", mid=MID)
    rec.dispatcher.run()
    assert len(rec.w.engine.repo.cases_for(MID)) == 1


def test_a_lost_heartbeat_opens_the_fault_case_not_the_watch(rec):
    rec.w.inject("E-LOST-1", "DEVICE_HEARTBEAT_LOST", mid=MID)
    rec.dispatcher.run()
    assert list(rec.cases()) == ["C2_SOUNDBOX_FAULT"]
