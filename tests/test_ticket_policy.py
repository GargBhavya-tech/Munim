"""#90 fewer tickets: answer or fix in the conversation first; a ticket only when a human or field action is needed."""
from core.engine.tickets import should_raise_ticket, ticket_rate_report

STEP = {"action": "raise_ticket"}


def test_how_to_question_never_makes_a_ticket():
    d = should_raise_ticket(STEP, {"intent_kind": "how_to", "needs_human": False})
    assert d.raise_ticket is False and d.code == "ANSWER_IN_CONVERSATION"
    # even when someone sets needs_human, a how-to is answered, not ticketed
    assert should_raise_ticket(STEP, {"intent_kind": "how_to", "needs_human": True}).raise_ticket is False


def test_fixed_in_the_conversation_makes_no_ticket():
    d = should_raise_ticket(STEP, {"intent_kind": "fault", "fixed_in_conversation": True, "needs_human": True})
    assert (d.raise_ticket, d.code) == (False, "FIXED_IN_CONVERSATION")


def test_human_or_field_action_needed_raises_with_reason():
    h = should_raise_ticket(STEP, {"intent_kind": "fault", "needs_human": True})
    f = should_raise_ticket(STEP, {"intent_kind": "fault", "needs_field": True})
    assert h.raise_ticket and f.raise_ticket
    assert "human action" in h.reason and "field action" in f.reason


def test_remote_steps_left_means_fix_first_not_ticket():
    d = should_raise_ticket(STEP, {"intent_kind": "fault", "remote_steps_remaining": 2})
    assert (d.raise_ticket, d.code) == (False, "FIX_FIRST")


def test_not_a_ticket_step_never_raises_and_a_message_is_not_a_trigger():
    assert should_raise_ticket({"action": "send_message"}, {"needs_human": True}).raise_ticket is False
    # every message must NOT make a ticket: a plain fault message with nothing needing a human
    assert should_raise_ticket(STEP, {"intent_kind": "fault"}).raise_ticket is False
    class Step:
        action = "raise_ticket"
    assert should_raise_ticket(Step(), {"needs_field": True}).raise_ticket is True


def test_ticket_rate_per_case_reported(conn):
    for i, pb in enumerate(["C2_SOUNDBOX_FAULT", "C2_SOUNDBOX_FAULT", "C2_SOUNDBOX_FAULT", "S1_ACTIVATION"]):
        conn.execute("INSERT INTO cases(case_id, merchant_id, playbook_id, state, lane, opened_at) VALUES (?,?,?,?,?,?)",
                     (f"CASE-X{i}", "M-0010", pb, "ACTIVE", "service", "2026-10-30T10:00:00+05:30"))
    conn.execute("INSERT INTO tickets VALUES ('T-X1','M-0010','DEVICE','OPEN','D-0010','x','2026-10-30T10:00:00+05:30','CASE-X0')")
    conn.execute("INSERT INTO tickets VALUES ('T-X2','M-0010','DEVICE','WITHDRAWN','D-0010','x','2026-10-30T10:00:00+05:30','CASE-X1')")
    conn.commit()
    r = ticket_rate_report(conn)
    assert (r["cases"], r["tickets"], r["rate"]) == (4, 1, 0.25)
    assert r["by_playbook"]["C2_SOUNDBOX_FAULT"] == {"cases": 3, "tickets": 1, "rate": 0.3333}
    assert r["by_playbook"]["S1_ACTIVATION"]["tickets"] == 0
