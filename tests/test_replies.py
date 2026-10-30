"""#103 reply handling and speed to lead, with the NO_REPLY / NOT_NOW retry timers.

Real policy, budget, ledger, executor and lifecycle; the transport is the recording ``StubHttp`` from test_outreach (the comms mock does not know leads yet).
"""
from datetime import timedelta

import pytest

from core.actions.executor import Executor
from core.engine.scripted import scripted_decider
from jobs.dedupe import DedupeJob
from jobs.lead_lifecycle import LeadLifecycle
from jobs.outreach import OutreachJob, build_first_contact
from jobs.replies import CLASSES, MAX_RETRIES, NO_REPLY_AFTER, NOT_NOW_DAYS, RETRY_AFTER, SLA_SECONDS, ReplyHandler, answer_question, classify, objection_answers
from tests.test_outreach import PH, StubHttp, grant, ready, state
from tests.test_scenarios_s1_c2 import SCRIPT
from tests.world import World

from jobs import replies as replies_mod


@pytest.fixture
def w(db_path, clock):
    w = World(db_path, clock, None, mid="M-0006")
    w.engine.decider = scripted_decider(SCRIPT)
    DedupeJob(w.conn, clock, w.ledger).run()
    return w


@pytest.fixture
def stub():
    return StubHttp()


@pytest.fixture
def stack(w, stub):
    ex = Executor(w.conn, w.clock, w.ledger, w.registry, stub, w.policy)
    life = LeadLifecycle(w.conn, w.clock, w.ledger, w.engine, w.timers)
    out = OutreachJob(w.conn, w.clock, w.ledger, ex, life, w.budget)
    return out, ReplyHandler(w.conn, w.clock, w.ledger, ex, life, out, w.budget)


@pytest.fixture
def contacted(w, stack, stub):
    """A consented offline lead that has had its first WhatsApp (state CONTACTED, silence check armed)."""
    out, _ = stack
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    assert out.first_contact(lid).status == "SENT"
    assert state(w, lid) == "CONTACTED"
    return lid


# ---------------------------------------------------------------------------------------------------------- classification
@pytest.mark.parametrize("text, cls", [
    ("stop", "unsubscribe"), ("Please unsubscribe me", "unsubscribe"), ("message mat bhejo", "unsubscribe"), ("स्टॉप", "unsubscribe"), ("ಕರೆ ಮಾಡಬೇಡಿ", "unsubscribe"),
    ("too costly, stop calling me", "unsubscribe"),                                    # WATCH OUT: an unsubscribe that also complains is NOT an objection
    ("your charges are too high", "objection"), ("I already have PhonePe", "objection"), ("not interested", "objection"),
    ("this is a scam", "angry"), ("tum log chor ho", "angry"),
    ("wrong number", "wrong_person"), ("sorry I am not the owner", "wrong_person"),
    ("I am out of station till Monday", "out_of_office"),
    ("not now, call later", "not_now"), ("abhi nahi, baad mein", "not_now"),
    ("tell me more", "interested"), ("haan", "interested"), ("yes", "interested"),
    ("how does the soundbox rent work?", "question"), ("kitna time lagega", "question"),
    ("hmm", "other"),
])
def test_reply_classes(text, cls):
    assert classify(text) == cls
    assert cls in CLASSES


def test_an_unsubscribe_is_never_filed_as_an_objection_whatever_else_it_says():
    for t in ("charges too high, unsubscribe", "already using PhonePe. stop", "mehenga hai, call mat karo", "not interested, do not contact me"):
        assert classify(t) == "unsubscribe", t


# ---------------------------------------------------------------------------------------------------------- unsubscribe is final and immediate
def test_unsubscribe_makes_the_lead_do_not_contact_at_once_and_nothing_is_sent(w, stack, stub, contacted):
    out, rep = stack
    sent_before = len(stub.calls)
    phone = w.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (contacted,)).fetchone()[0]
    assert rep.pending(contacted)                                                       # the silence check was armed by the first contact
    res = rep.handle(contacted, "please stop messaging me", "whatsapp")
    assert (res.cls, res.action, res.lead_state) == ("unsubscribe", "DO_NOT_CONTACT", "DO_NOT_CONTACT")
    assert len(stub.calls) == sent_before                                               # no goodbye, no pitch
    assert w.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (phone,)).fetchone()
    assert {r["status"] for r in w.conn.execute("SELECT status FROM consent WHERE subject_id = ?", (contacted,))} == {"REVOKED"}
    assert rep.pending(contacted) == []                                                 # retries cancelled
    # nothing can bring it back: a long time later, no timer fires a message and a direct attempt is refused
    w.clock.advance(timedelta(days=60))
    assert len(stub.calls) == sent_before
    assert out.first_contact(contacted).status == "NOT_ELIGIBLE"


def test_an_unsubscribe_from_a_lead_with_a_future_not_now_retry_cancels_it(w, stack, stub, contacted):
    _, rep = stack
    rep.handle(contacted, "not now", "whatsapp")
    assert [p["kind"] for p in rep.pending(contacted)] == ["NOT_NOW"]
    n = len(stub.calls)                                                                 # first contact + the thank-you
    rep.handle(contacted, "stop", "whatsapp", received_at=w.clock.now() + timedelta(hours=1))
    assert rep.pending(contacted) == [] and state(w, contacted) == "DO_NOT_CONTACT"
    w.clock.advance(timedelta(days=NOT_NOW_DAYS + 1))
    assert len(stub.calls) == n


def test_the_same_inbound_message_delivered_twice_is_handled_once(w, stack, stub, contacted):
    _, rep = stack
    t = w.clock.now()
    a = rep.handle(contacted, "tell me more", "whatsapp", received_at=t)
    b = rep.handle(contacted, "tell me more", "whatsapp", received_at=t)
    assert a.action == "ENGAGED" and b.action == "DUPLICATE"
    assert len(stub.calls) == 2                                                         # first contact + ONE acknowledgement
    assert w.conn.execute("SELECT COUNT(*) FROM lead_replies WHERE lead_id = ?", (contacted,)).fetchone()[0] == 1


# ---------------------------------------------------------------------------------------------------------- speed to lead
def test_an_interested_reply_is_acknowledged_within_the_sla_and_the_lead_is_engaged(w, stack, stub, contacted):
    _, rep = stack
    received = w.clock.now() - timedelta(seconds=90)
    res = rep.handle(contacted, "haan, batao", "whatsapp", received_at=received)
    assert (res.cls, res.action, res.lead_state, res.ack_status) == ("interested", "ENGAGED", "ENGAGED", "SENT")
    assert res.ack_seconds == 90 and res.sla_met is True and SLA_SECONDS == 300
    method, path, body, _key = stub.calls[-1]
    assert body["body"] == PH.text("one_moment", "en-IN") and body["channel"] == "whatsapp"


def test_a_slow_acknowledgement_is_recorded_as_missing_the_sla(w, stack, contacted):
    _, rep = stack
    res = rep.handle(contacted, "yes", "whatsapp", received_at=w.clock.now() - timedelta(minutes=12))
    assert res.sla_met is False and res.ack_seconds == 720
    assert w.conn.execute("SELECT sla_met FROM lead_replies WHERE reply_id = ?", (res.reply_id,)).fetchone()[0] == 0


def test_a_reply_at_night_is_still_answered_because_it_is_not_proactive_contact(w, stack, stub, contacted):
    _, rep = stack
    w.clock.advance(timedelta(hours=13))                                                # 22:00: proactive contact is blocked, a reply is not
    res = rep.handle(contacted, "interested", "whatsapp")
    assert res.ack_status == "SENT"


def test_a_reply_without_consent_on_that_channel_is_not_answered(w, stack, stub, contacted):
    _, rep = stack
    n = len(stub.calls)
    res = rep.handle(contacted, "interested", "sms")                                    # consent is for WhatsApp only
    assert res.ack_status == "REFUSED" and len(stub.calls) == n


# ---------------------------------------------------------------------------------------------------------- the other classes
def test_an_angry_lead_goes_to_a_person_and_is_not_pitched(w, stack, stub, contacted):
    _, rep = stack
    res = rep.handle(contacted, "this is a scam, stop harassing", "whatsapp")        # also an unsubscribe: stop wins
    assert res.cls == "unsubscribe"
    lid2 = ready(w, "offline", dnd=False)
    grant(w, lid2, "whatsapp")
    rep.outreach.first_contact(lid2)
    res = rep.handle(lid2, "you people are a fraud", "whatsapp")
    assert (res.cls, res.action) == ("angry", "HANDOFF")
    assert stub.calls[-1][2]["body"] == PH.text("handoff", "en-IN")
    esc = [dict(r) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'ESCALATION'")]
    assert any(lid2 in e["payload_json"] for e in esc)
    assert rep.pending(lid2) == []


def test_a_wrong_person_is_disqualified_and_not_contacted_again(w, stack, stub, contacted):
    out, rep = stack
    res = rep.handle(contacted, "wrong number", "whatsapp")
    assert (res.cls, res.lead_state, res.action) == ("wrong_person", "DISQUALIFIED", "DISQUALIFIED") and res.ack_status is None
    assert rep.pending(contacted) == []
    assert out.first_contact(contacted).status == "NOT_ELIGIBLE"


def test_a_not_now_reply_parks_the_lead_and_one_retry_goes_out_later(w, stack, stub, contacted):
    _, rep = stack
    res = rep.handle(contacted, "not now, busy", "whatsapp")
    assert (res.cls, res.lead_state) == ("not_now", "NURTURE")
    assert rep.pending(contacted)[0]["due_at"].startswith("2026-11-14T09:00")          # 14 days after 31 Oct, 09:00
    assert [h[1] for h in LeadLifecycle(w.conn, w.clock, w.ledger).history(contacted)][-3:] == ["CONTACTED", "NOT_NOW", "NURTURE"]
    n = len(stub.calls)
    w.clock.advance(timedelta(days=NOT_NOW_DAYS - 1))
    assert len(stub.calls) == n                                                          # not before the date
    w.clock.advance(timedelta(days=1, minutes=1))
    assert len(stub.calls) == n + 1 and state(w, contacted) == "CONTACTED"
    assert stub.calls[-1][2]["body"] == build_first_contact("en-IN", PH)                 # the identity and PIN/OTP lines go out again


def test_a_retry_the_contact_budget_refuses_is_tried_again_at_the_next_ten_oclock(w, stack, stub, contacted):
    _, rep = stack
    w.clock.advance(timedelta(hours=12, minutes=30))                                     # 21:30: quiet hours
    rep.handle(contacted, "not now", "whatsapp")
    n = len(stub.calls)
    w.clock.advance(timedelta(days=NOT_NOW_DAYS, minutes=1))                             # due at 21:30 on the 14th: quiet, so deferred
    assert len(stub.calls) == n and rep.pending(contacted)[0]["due_at"].startswith("2026-11-15T10:00")
    w.clock.advance(timedelta(days=1, hours=13))
    assert len(stub.calls) == n + 1 and state(w, contacted) == "CONTACTED"


def test_an_out_of_office_reply_retries_after_a_few_days_without_a_pitch(w, stack, stub, contacted):
    _, rep = stack
    n = len(stub.calls)
    res = rep.handle(contacted, "I am out of station", "whatsapp")
    assert (res.cls, res.lead_state, res.ack_status) == ("out_of_office", "NURTURE", None) and len(stub.calls) == n
    w.clock.advance(timedelta(days=3, minutes=1))
    assert len(stub.calls) == n + 1


def test_an_objection_returns_the_approved_answer_as_a_kb_source_and_a_person_if_none_exists(w, stack, contacted):
    _, rep = stack
    res = rep.handle(contacted, "your charges are too high", "whatsapp")
    assert (res.cls, res.action, res.lead_state) == ("objection", "OBJECTION_ANSWER", "ENGAGED")
    assert [s.source_id for s in res.sources] == ["kb:objection_answers#charges_too_high@v1"]
    assert "rupees per payment" in res.sources[0].text
    assert objection_answers("not interested") == []                                     # no approved answer for a flat no: a person decides


def test_a_question_is_answered_only_from_approved_docs(w, stack, contacted):
    _, rep = stack
    a = answer_question("How does the soundbox plan and device rent work")
    assert a is not None and a.source_id.startswith("kb:") and a.overlap >= 2
    assert answer_question("what is the capital of France") is None
    res = rep.handle(contacted, "what is the capital of France?", "whatsapp")
    assert (res.cls, res.action, res.sources, res.ack_status) == ("question", "HANDOFF", [], None)


# ---------------------------------------------------------------------------------------------------------- NO_REPLY retries
def test_silence_becomes_no_reply_then_two_retries_then_nurture(w, stack, stub, contacted):
    out, rep = stack
    assert len(stub.calls) == 1
    w.clock.advance(NO_REPLY_AFTER + timedelta(minutes=1))
    assert state(w, contacted) == "NO_REPLY" and len(stub.calls) == 1
    w.clock.advance(RETRY_AFTER)
    assert state(w, contacted) == "CONTACTED" and len(stub.calls) == 2                  # retry 1
    w.clock.advance(NO_REPLY_AFTER + timedelta(minutes=1))
    assert state(w, contacted) == "NO_REPLY"
    w.clock.advance(RETRY_AFTER)
    assert state(w, contacted) == "CONTACTED" and len(stub.calls) == 3                  # retry 2 (MAX_RETRIES == 2)
    w.clock.advance(NO_REPLY_AFTER + timedelta(minutes=1))
    assert state(w, contacted) == "NURTURE" and MAX_RETRIES == 2
    w.clock.advance(timedelta(days=90))
    assert len(stub.calls) == 3                                                          # no fourth contact
    assert rep.pending(contacted) == []


def test_a_reply_before_the_silence_check_cancels_it(w, stack, stub, contacted):
    _, rep = stack
    w.clock.advance(timedelta(hours=24))
    rep.handle(contacted, "interested", "whatsapp")
    n = len(stub.calls)
    w.clock.advance(timedelta(days=20))
    assert state(w, contacted) == "ENGAGED" and len(stub.calls) == n


def test_a_retry_never_fires_for_a_lead_that_went_on_dnd_meanwhile(w, stack, stub, contacted):
    _, rep = stack
    w.clock.advance(NO_REPLY_AFTER + timedelta(minutes=1))
    assert state(w, contacted) == "NO_REPLY"
    phone = w.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (contacted,)).fetchone()[0]
    w.conn.execute("INSERT INTO dnd_registry(phone) VALUES (?)", (phone,))              # the number was registered while we waited
    w.clock.advance(RETRY_AFTER + timedelta(days=30))
    assert len(stub.calls) == 1


def test_pending_retries_survive_a_restart(w, stack, stub, contacted, db_path, clock):
    out, rep = stack
    ex = out.executor
    # a new process: new handler, new clock handles. Overdue retries fire on rearm, exactly once.
    clock._heap.clear()
    clock._callbacks.clear()
    rep2 = ReplyHandler(w.conn, clock, w.ledger, ex, out.life, out, w.budget)
    assert rep2.rearm() == 1
    clock.advance(NO_REPLY_AFTER + timedelta(minutes=1))
    assert state(w, contacted) == "NO_REPLY"
    assert w.conn.execute("SELECT COUNT(*) FROM lead_retries WHERE kind = 'NO_REPLY_CHECK' AND status = 'FIRED'").fetchone()[0] == 1


def test_there_is_no_wall_clock_in_the_new_modules():
    import re
    from pathlib import Path
    for f in ("outreach", "replies"):
        src = Path(__file__).resolve().parent.parent.joinpath("jobs", f"{f}.py").read_text(encoding="utf-8")
        assert not re.search(r"datetime\.now|utcnow|date\.today|time\.time\(|CURRENT_TIMESTAMP|datetime\('now'\)", src), f
    assert replies_mod.RETRY_HOUR == 10
