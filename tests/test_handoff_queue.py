"""#55 handoff queue, human actions, return-to-agent round trip, verdicts."""
import json
from datetime import timedelta

import pytest

from core.actions.types import Principal
from core.engine.events import count_events
from core.engine.handoff import (HandoffError, HandoffPermissionError, HandoffService, HandoffStateError, repeats_last_message)
from core.engine.workflow import Decision
from tests.world import World

MID = "M-0006"
RISK = Principal("HUMAN", "asha", role="risk_reviewer")
SUPPORT = Principal("HUMAN", "ravi", role="support_executive")
SALES = Principal("HUMAN", "meena", role="sales_lead")
ADMIN = Principal("HUMAN", "boss", role="ops_admin")
AUDITOR = Principal("HUMAN", "aud", role="auditor")
BOT = Principal("AGENT", "munim")


class Recorder:
    """Stand-in executor: records what a human asked for, as which principal."""
    def __init__(self):
        self.calls = []

    def submit(self, intent):
        self.calls.append(("submit", intent))
        from core.actions.types import ActionOutcome
        return ActionOutcome("OK", f"k{len(self.calls)}")

    def undo(self, action_id, principal):
        self.calls.append(("undo", action_id, principal))
        from core.actions.types import ActionOutcome
        return ActionOutcome("OK", "u")


def mk(world, mid, lane="P1", playbook="S1_ACTIVATION"):
    return world.engine.repo.open_case(playbook_id=playbook, lane=lane, merchant_id=mid)


def escalate(world, svc, case, reasons, **kw):
    c = world.engine.escalate(world.engine.repo.load(case.case_id), list(reasons))
    return svc.create_packet(c, reasons, **kw)


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock)


@pytest.fixture
def svc(world):
    return HandoffService(world.conn, world.clock, world.ledger)


# ------------------------------------------------------------------ ordering
def test_queue_is_ordered_by_deadline_earliest_first_not_by_creation(world, svc):
    a = escalate(world, svc, mk(world, "M-0001"), ["LOOP"])                       # 4h
    b = escalate(world, svc, mk(world, "M-0002"), ["SETTLEMENT_HELD_RISK"])       # 2h  -> earlier deadline although created later
    c = escalate(world, svc, mk(world, "M-0003"), ["AMOUNT_ABOVE_LIMIT"])         # 4h
    assert [h.handoff_id for h in svc.list(status="OPEN")] == [b.handoff_id, a.handoff_id, c.handoff_id]


def test_equal_deadlines_break_by_lane_then_age(world, svc):
    # all three end up with the same deadline (t0 + 4h): the later ones have a shorter window
    p2 = escalate(world, svc, mk(world, "M-0001", lane="P2"), ["LOOP"])                      # t0, 4h, P2
    p0_old = escalate(world, svc, mk(world, "M-0002", lane="P0"), ["LOOP"])                  # t0, 4h, P0
    world.clock.advance(timedelta(hours=2))
    p0_new = escalate(world, svc, mk(world, "M-0003", lane="P0"), ["SETTLEMENT_HELD_RISK"])  # t0+2h, 2h, P0
    assert p2.deadline == p0_old.deadline == p0_new.deadline
    assert [h.handoff_id for h in svc.list()] == [p0_old.handoff_id, p0_new.handoff_id, p2.handoff_id]


def test_list_filters_by_role_and_status(world, svc):
    r = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    s = escalate(world, svc, mk(world, "M-0002"), ["WANTS_HUMAN"])
    assert [h.handoff_id for h in svc.list("risk_reviewer")] == [r.handoff_id]
    assert [h.handoff_id for h in svc.list("support_executive")] == [s.handoff_id]
    assert {h.handoff_id for h in svc.list("ops_admin")} == {r.handoff_id, s.handoff_id}       # the supervisor sees every queue
    svc.close(r.handoff_id, RISK)
    assert svc.list("risk_reviewer") == []
    assert [h.handoff_id for h in svc.list("risk_reviewer", status="CLOSED")] == [r.handoff_id]
    assert {h.status for h in svc.list(status=None)} == {"OPEN", "CLOSED"}


# ------------------------------------------------------------------ who may act
def test_a_role_cannot_return_a_case_outside_its_queue(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])      # risk_reviewer's queue
    before = (count_events(world.conn, "HUMAN_RETURNED"), len(world.ledger.entries()))
    for who in (SUPPORT, SALES, AUDITOR, BOT, Principal("HUMAN", "x", role=None), Principal("HUMAN", "x", role="intern")):
        with pytest.raises(HandoffPermissionError):
            svc.return_to_agent(h.handoff_id, who, "continue")
    assert (count_events(world.conn, "HUMAN_RETURNED"), len(world.ledger.entries())) == before     # nothing happened at all
    assert svc.get(h.handoff_id).status == "OPEN"
    assert world.engine.repo.load(h.case_id).state == "ESCALATED"


def test_the_owner_role_and_the_supervisor_may_return(world, svc):
    h1 = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    h2 = escalate(world, svc, mk(world, "M-0002"), ["SETTLEMENT_HELD_RISK"])
    assert svc.return_to_agent(h1.handoff_id, RISK, "continue")["type"] == "HUMAN_RETURNED"
    assert svc.return_to_agent(h2.handoff_id, ADMIN, "continue")["type"] == "HUMAN_RETURNED"


def test_other_actions_are_role_checked_too(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    svc.executor = rec = Recorder()
    for call in (lambda p: svc.reply(h.handoff_id, p, "hello"), lambda p: svc.act(h.handoff_id, p, "raise_ticket", {}),
                 lambda p: svc.undo(h.handoff_id, p, "ACT-1"), lambda p: svc.close(h.handoff_id, p),
                 lambda p: svc.record_verdict(h.handoff_id, "needed", p)):
        with pytest.raises(HandoffPermissionError):
            call(SALES)
    assert rec.calls == [] and svc.get(h.handoff_id).status == "OPEN" and svc.get(h.handoff_id).verdict is None


def test_return_needs_an_instruction_an_open_handoff_and_an_escalated_case(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    for blank in ("", "   ", None):
        with pytest.raises(HandoffError):
            svc.return_to_agent(h.handoff_id, RISK, blank)
    svc.return_to_agent(h.handoff_id, RISK, "continue")
    with pytest.raises(HandoffStateError):
        svc.return_to_agent(h.handoff_id, RISK, "continue again")        # already RETURNED
    assert count_events(world.conn, "HUMAN_RETURNED") == 1
    h2 = svc.create_packet(mk(world, "M-0002"), ["LOOP"], [])             # a handoff whose case is not ESCALATED
    with pytest.raises(HandoffStateError):
        svc.return_to_agent(h2.handoff_id, SUPPORT, "continue")


# ------------------------------------------------------------------ return: ledger, status, event
def test_return_records_human_action_status_and_event_with_the_instruction(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    ev = svc.return_to_agent(h.handoff_id, RISK, "  continue; replacement approved  ")
    assert svc.get(h.handoff_id).status == "RETURNED"
    ha = [e for e in world.ledger.entries(h.case_id) if e.kind == "HUMAN_ACTION"]
    assert len(ha) == 1 and (ha[0].actor, ha[0].role) == ("HUMAN:asha", "risk_reviewer")
    assert ha[0].payload == {"handoff_id": h.handoff_id, "op": "return", "instruction": "continue; replacement approved"}
    row = world.conn.execute("SELECT * FROM events WHERE event_id = ?", (ev["event_id"],)).fetchone()
    assert (row["type"], row["case_id"], row["merchant_id"]) == ("HUMAN_RETURNED", h.case_id, "M-0001")
    assert json.loads(row["payload_json"])["instruction"] == "continue; replacement approved"
    assert ev["payload"]["returned_by"] == "asha" and ev["payload"]["handoff_id"] == h.handoff_id


def test_on_return_hook_receives_the_event_after_commit(world):
    seen = []
    svc = HandoffService(world.conn, world.clock, world.ledger, on_return=lambda e: seen.append((e["event_id"], svc.get(e["payload"]["handoff_id"]).status)))
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    ev = svc.return_to_agent(h.handoff_id, RISK, "continue")
    assert seen == [(ev["event_id"], "RETURNED")]


# ------------------------------------------------------------------ the round trip (the ticket's Test and the Watch out)
def test_round_trip_agent_human_agent_resumes_with_the_note_and_does_not_repeat(db_path, clock):
    seen = []

    def decide(ctx):
        seen.append(ctx)
        if ctx.event.type == "HUMAN_RETURNED":
            note, last = ctx.event.payload["instruction"], ctx.event.payload["do_not_repeat"]
            body = "Your replacement is approved and on its way." if "replacement approved" in note else "Noted."
            assert not repeats_last_message(body, last)
            return Decision(message_en=body, channel="whatsapp")
        return Decision(message_en="Please check that the Soundbox is switched on.", channel="whatsapp")

    w = World(db_path, clock, {("S1_ACTIVATION", "kyc"): decide})
    svc = HandoffService(w.conn, clock, w.ledger, on_return=w.engine.handle_event)
    case = w.engine.open_case(MID, "S1_ACTIVATION", language="en")

    # turn 1: the agent speaks, then the case is escalated
    w.engine.handle_event({"event_id": "E-1", "type": "MERCHANT_MESSAGE", "merchant_id": MID, "case_id": case.case_id,
                           "payload": {"text": "soundbox is dead"}})
    first = svc.last_agent_message(case.case_id)
    assert first == "Please check that the Soundbox is switched on."
    h = escalate(w, svc, case, ["PLAYBOOK_ESCALATE_WHEN: replacements_last_30d >= 1"])
    assert w.engine.repo.load(case.case_id).state == "ESCALATED"

    # a merchant message while escalated is logged but the agent does not answer
    clock.advance(timedelta(minutes=30))
    w.engine.handle_event({"event_id": "E-2", "type": "MERCHANT_MESSAGE", "merchant_id": MID, "case_id": case.case_id, "payload": {"text": "hello?"}})
    assert len(seen) == 1

    # human returns with an instruction -> next turn
    clock.advance(timedelta(hours=1))
    ev = svc.return_to_agent(h.handoff_id, SUPPORT, "continue; replacement approved")

    assert len(seen) == 2, "the engine ran a new turn"
    ctx = seen[-1]
    assert ctx.event.type == "HUMAN_RETURNED" and ctx.event.event_id == ev["event_id"]
    assert ctx.event.payload["instruction"] == "continue; replacement approved"        # the note is in the next turn's context
    assert ctx.facts["event"]["instruction"] == "continue; replacement approved"       # ... and in the facts the model sees
    assert ctx.facts["event_type"] == "HUMAN_RETURNED"
    assert ctx.step.name == "kyc"                                                      # resumed at diagnose, at the unmet step
    states = [e.payload["state"] for e in w.ledger.entries(case.case_id) if e.kind == "STATE"]
    assert states[states.index("ESCALATED"):][:3] == ["ESCALATED", "RETURNED_TO_AGENT", "ACTIVE"]
    sent = [e.payload["text"] for e in w.ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"]
    assert sent == [first, "Your replacement is approved and on its way."]           # the returned case did not repeat its last message
    assert ctx.event.payload["do_not_repeat"] == first


def test_repeat_guard_ignores_case_spacing_and_punctuation():
    last = "Please check that the Soundbox is switched on."
    assert repeats_last_message("please  check that the soundbox is switched on", last)
    assert repeats_last_message(last.upper() + "!!", last)
    assert not repeats_last_message("Your replacement is approved.", last)
    assert not repeats_last_message("anything", None) and not repeats_last_message(None, last) and not repeats_last_message("", "")


def test_return_payload_carries_the_last_agent_message_not_a_human_one(world, svc):
    c = mk(world, "M-0001")
    world.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=c.case_id, payload={"language": "en", "text": "first"})
    world.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=c.case_id, payload={"language": "en", "text": "second"})
    h = escalate(world, svc, c, ["LOOP"])
    assert svc.return_to_agent(h.handoff_id, SUPPORT, "go on")["payload"]["do_not_repeat"] == "second"
    c2 = mk(world, "M-0002")
    h2 = escalate(world, svc, c2, ["LOOP"])
    assert svc.return_to_agent(h2.handoff_id, SUPPORT, "go on")["payload"]["do_not_repeat"] is None


# ------------------------------------------------------------------ reply / act / undo / close
def test_reply_and_act_go_through_the_executor_as_the_human(world, svc):
    svc.executor = rec = Recorder()
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    svc.reply(h.handoff_id, RISK, "Your settlement is under review.", language="kn")
    svc.act(h.handoff_id, RISK, "raise_ticket", {"merchant_id": "M-0001"})
    svc.undo(h.handoff_id, RISK, "ACT-0007")
    kinds = [c[0] for c in rec.calls]
    assert kinds == ["submit", "submit", "undo"]
    reply, act = rec.calls[0][1], rec.calls[1][1]
    assert reply.action == "send_message" and reply.principal == RISK and reply.args["body"] == "Your settlement is under review."
    assert reply.args["language"] == "kn" and reply.args["merchant_id"] == "M-0001"
    assert act.action == "raise_ticket" and act.principal == RISK and act.case_id == h.case_id
    assert reply.step_seq != act.step_seq                                         # distinct idempotency keys
    assert rec.calls[2][1:] == ("ACT-0007", RISK)
    ops = [e.payload["op"] for e in world.ledger.entries(h.case_id) if e.kind == "HUMAN_ACTION"]
    assert ops == ["reply", "act", "undo"]


def test_a_human_action_without_an_executor_is_refused_not_silently_dropped(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["SETTLEMENT_HELD_RISK"])
    with pytest.raises(HandoffError):
        svc.reply(h.handoff_id, RISK, "hi")


def test_human_undo_through_the_real_executor(world):
    svc = HandoffService(world.conn, world.clock, world.ledger, executor=world.executor)
    c = mk(world, MID)
    h = escalate(world, svc, c, ["WANTS_HUMAN"])
    out = svc.act(h.handoff_id, SUPPORT, "raise_ticket", {"merchant_id": MID, "type": "SETTLEMENT", "system_object": "settlement", "summary": "follow up", "case_id": c.case_id})
    assert out.status == "OK", out.error
    assert world.conn.execute("SELECT status FROM tickets WHERE ticket_id = ?", (out.result["ticket_id"],)).fetchone()[0] == "OPEN"
    undone = svc.undo(h.handoff_id, SUPPORT, out.action_id)
    assert undone.status == "OK", undone.error
    assert world.conn.execute("SELECT status FROM tickets WHERE ticket_id = ?", (out.result["ticket_id"],)).fetchone()[0] != "OPEN"


def test_close_resolves_the_case_and_ends_the_handoff(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["WANTS_HUMAN"])
    done = svc.close(h.handoff_id, SUPPORT, note="called the merchant, sorted")
    assert done.status == "CLOSED" and world.engine.repo.load(h.case_id).state == "RESOLVED"
    assert [e.payload["op"] for e in world.ledger.entries(h.case_id) if e.kind == "HUMAN_ACTION"] == ["close"]
    with pytest.raises(HandoffStateError):
        svc.return_to_agent(h.handoff_id, SUPPORT, "too late")
    with pytest.raises(HandoffStateError):
        svc.close(h.handoff_id, SUPPORT)


# ------------------------------------------------------------------ verdict
def test_verdict_is_stored_validated_and_feeds_precision(world, svc):
    hs = [escalate(world, svc, mk(world, f"M-000{i}"), ["WANTS_HUMAN"]) for i in range(1, 5)]
    assert svc.escalation_precision() is None
    svc.record_verdict(hs[0].handoff_id, "needed")
    svc.record_verdict(hs[1].handoff_id, "needed", SUPPORT)
    svc.record_verdict(hs[2].handoff_id, "needed")
    svc.record_verdict(hs[3].handoff_id, "not_needed")
    assert [svc.get(h.handoff_id).verdict for h in hs] == ["needed", "needed", "needed", "not_needed"]
    assert svc.escalation_precision() == 0.75
    with pytest.raises(ValueError):
        svc.record_verdict(hs[0].handoff_id, "maybe")
    assert svc.get(hs[0].handoff_id).verdict == "needed"
    assert [e.payload["verdict"] for e in world.ledger.entries(hs[1].case_id) if e.kind == "HUMAN_ACTION"] == ["needed"]


def test_verdict_can_be_given_after_the_handoff_was_returned(world, svc):
    h = escalate(world, svc, mk(world, "M-0001"), ["WANTS_HUMAN"])
    svc.return_to_agent(h.handoff_id, SUPPORT, "continue")
    assert svc.record_verdict(h.handoff_id, "not_needed", SUPPORT).verdict == "not_needed"
