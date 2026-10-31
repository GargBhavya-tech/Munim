"""Routing of a merchant's message to a case: a bare greeting is greeted (not handed over), and a new topic on a merchant whose only
cases are with a person gets a case of its own. The engine's event handling is replaced by a recorder: this file tests only WHERE a message goes."""
import pytest

from core.engine.dispatcher import Dispatcher, is_greeting
from core.engine.intents import INTENT_TO_PLAYBOOK
from tests.world import World

MID = "M-0006"
DEVICE_INTENT = "REPORT_DEVICE_PROBLEM"


class Recorder:
    def __init__(self, world):
        self.world, self.seen = world, []
        world.engine.handle_event = lambda ev: self.seen.append((ev.event_id, ev.case_id)) or None
        self.dispatcher = Dispatcher(world.conn, world.engine, world.playbooks)

    def say(self, text, event_id, intent=None):
        payload = {"text": text}
        if intent:
            payload["intent"] = intent
        self.world.inject(event_id, "MERCHANT_MESSAGE", mid=MID, payload=payload)
        return self.dispatcher.run()

    def cases(self):
        return {c.playbook_id: c for c in self.world.engine.repo.cases_for(MID)}


@pytest.fixture
def rec(db_path, clock):
    return Recorder(World(db_path, clock, mid=MID))


def escalate(rec, playbook_id):
    repo = rec.world.engine.repo
    case = rec.world.engine.open_case(MID, playbook_id, language="en")
    case = repo.transition(case, "ACTIVE", reason="test")
    return repo.transition(case, "ESCALATED", reason="test")


# ------------------------------------------------------------------------------------------------------------ greetings
@pytest.mark.parametrize("text", ["hi", "Hi!", "hello", "  HELLO  ", "hey", "namaste", "नमस्ते", "ನಮಸ್ಕಾರ", "hii", "Good morning"])
def test_a_bare_greeting_is_a_greeting(text):
    assert is_greeting(text)


@pytest.mark.parametrize("text", ["hello how are you", "hi my settlement has not come", "", "ok", "help", "hello?? where is my money", "hi 500"])
def test_anything_beyond_a_greeting_is_not(text):
    assert not is_greeting(text)


def test_a_first_hi_opens_the_greeting_case_not_the_unsupported_one(rec):
    (d,) = rec.say("hi", "E-1")
    assert d.opened and list(rec.cases()) == ["G0_GREETING"]


def test_a_first_message_that_only_looks_like_one_is_still_unsupported(rec):
    rec.say("hello how are you", "E-1")
    assert list(rec.cases()) == ["X0_UNSUPPORTED"]


def test_a_greeting_is_not_a_new_case_when_the_merchant_has_a_case_open(rec):
    case = rec.world.engine.open_case(MID, "C2_SOUNDBOX_FAULT", language="en")
    rec.say("hi", "E-1")
    assert list(rec.cases()) == ["C2_SOUNDBOX_FAULT"] and rec.seen == [("E-1", case.case_id)]


def test_a_greeting_to_a_merchant_whose_case_is_with_a_person_stays_with_that_case(rec):
    held = escalate(rec, "C1_SETTLEMENT_DELAY")
    rec.say("hello", "E-1")
    assert list(rec.cases()) == ["C1_SETTLEMENT_DELAY"] and rec.seen == [("E-1", held.case_id)]


# ------------------------------------------------------------------------------------------------------------ a new topic
def test_the_device_intent_maps_to_the_soundbox_playbook():
    assert INTENT_TO_PLAYBOOK[DEVICE_INTENT] == "C2_SOUNDBOX_FAULT"


def test_a_new_topic_after_a_hand_over_gets_its_own_case_and_the_held_case_is_left_alone(rec):
    held = escalate(rec, "X0_UNSUPPORTED")
    (d,) = rec.say("my soundbox is not working", "E-1", intent=DEVICE_INTENT)
    opened = rec.cases()["C2_SOUNDBOX_FAULT"]
    assert d.opened and d.case_id == opened.case_id
    assert rec.seen == [("E-1", opened.case_id)]                            # the escalated case is NOT sent the event (no second hand-over line)
    assert rec.world.engine.repo.load(held.case_id).state == "ESCALATED"


def test_the_same_topic_after_a_hand_over_stays_with_the_person(rec):
    held = escalate(rec, "C2_SOUNDBOX_FAULT")
    rec.say("my soundbox is not working", "E-1", intent=DEVICE_INTENT)
    assert list(rec.cases()) == ["C2_SOUNDBOX_FAULT"] and rec.seen == [("E-1", held.case_id)]


def test_an_unsupported_message_after_a_hand_over_stays_with_the_person(rec):
    held = escalate(rec, "C1_SETTLEMENT_DELAY")
    rec.say("what is the weather", "E-1")
    assert list(rec.cases()) == ["C1_SETTLEMENT_DELAY"] and rec.seen == [("E-1", held.case_id)]


def test_a_new_topic_does_not_leave_a_case_that_is_actively_being_worked(rec):
    active = rec.world.engine.open_case(MID, "C1_SETTLEMENT_DELAY", language="en")
    rec.say("my soundbox is not working", "E-1", intent=DEVICE_INTENT)
    assert list(rec.cases()) == ["C1_SETTLEMENT_DELAY"] and rec.seen == [("E-1", active.case_id)]      # unchanged: the model raises a second topic itself


def test_a_new_topic_with_one_case_held_and_one_active_does_not_open_a_third(rec):
    escalate(rec, "X0_UNSUPPORTED")
    active = rec.world.engine.open_case(MID, "C1_SETTLEMENT_DELAY", language="en")
    rec.say("my soundbox is not working", "E-1", intent=DEVICE_INTENT)
    assert set(rec.cases()) == {"X0_UNSUPPORTED", "C1_SETTLEMENT_DELAY"}
    assert active.case_id in {c for _, c in rec.seen}                       # the case being worked still hears the message, as before
