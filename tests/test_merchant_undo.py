"""#65: "wapas karo" undoes only THIS case's last compensable action, through the executor; receipts show what can still be cancelled."""
from datetime import timedelta

import pytest

from core.actions.types import ActionIntent
from core.engine import merchant_undo
from tests.test_turn_assembly import MID, Talk


@pytest.fixture
def t(db_path, clock, monkeypatch):
    from core.voice import language as L
    monkeypatch.setattr(L.LanguageManager, "current", lambda self, mid, case_language=None: "hinglish")
    return Talk(db_path, clock, [])


def follow_up(t, case, seq, due="2026-10-31T10:00:00+05:30"):
    out = t.w.executor.submit(ActionIntent("schedule_follow_up", {"merchant_id": MID, "due_at": due, "case_id": case.case_id}, case_id=case.case_id,
                                           step_seq=seq, allowlist=("schedule_follow_up",)))
    assert out.status == "OK", out.error
    return out.action_id


def compensated(t):
    return {r["action_id"] for r in t.w.conn.execute("SELECT action_id FROM compensations WHERE status = 'DONE'")}


def test_wapas_karo_undoes_only_this_cases_last_action(t):
    c1 = t.w.engine.open_case(MID, "C1_SETTLEMENT_DELAY")          # P1: the focus of an undo with no playbook of its own
    c9 = t.w.engine.open_case(MID, "C9_COMPLAINT")
    a1 = follow_up(t, c1, 1)
    a2 = follow_up(t, c1, 2, "2026-11-01T10:00:00+05:30")
    other = follow_up(t, c9, 1)
    t.say("wapas karo", intent="UNDO_LAST")
    assert compensated(t) == {a2}                                   # the LAST one, of THIS case
    assert t.agent.last.path == "UNDO_DONE" and t.sent()[-1]["text"] == "Ho gaya. Maine ise cancel kar diya: Follow-up set ho gaya."
    t.say("ek aur wapas karo", intent="UNDO_LAST")
    assert compensated(t) == {a2, a1} and other not in compensated(t)
    t.say("wapas karo", intent="UNDO_LAST")
    assert compensated(t) == {a2, a1} and t.agent.last.path == "UNDO_NOTHING"
    assert t.sent()[-1]["text"] == "Is request mein abhi cancel karne ke liye kuch nahi hai."
    assert t.provider.seen == []                                    # the model is never asked to undo


def test_an_expired_window_is_not_undone(t):
    c2 = t.w.engine.open_case(MID, "C2_SOUNDBOX_FAULT")
    out = t.w.executor.submit(ActionIntent("create_device_replacement", {"merchant_id": MID, "reason": "NO_ANNOUNCEMENT"}, case_id=c2.case_id, step_seq=1,
                                           allowlist=("create_device_replacement",)))
    assert out.status == "OK", out.error
    assert [r["action_id"] for r in merchant_undo.open_undoable(t.w.conn, t.w.clock, c2.case_id)] == [out.action_id]
    t.w.clock.advance(timedelta(hours=25))                         # the replacement's 24-hour undo window has closed
    assert merchant_undo.open_undoable(t.w.conn, t.w.clock, c2.case_id) == []
    t.say("wapas karo", intent="UNDO_LAST")
    assert compensated(t) == set() and t.agent.last.path == "UNDO_NOTHING"


def test_receipts_list_this_cases_undoable_actions_in_the_case_language(t):
    c1 = t.w.engine.open_case(MID, "C1_SETTLEMENT_DELAY")
    c9 = t.w.engine.open_case(MID, "C9_COMPLAINT")
    a1 = follow_up(t, c1, 1)
    follow_up(t, c9, 1)
    r = merchant_undo.receipts(t.w.conn, t.w.clock, c1.case_id, "en-IN")
    assert [x.action_id for x in r] == [a1] and r[0].label == "Follow-up scheduled" and r[0].cancel.startswith("cancel ")
    assert merchant_undo.receipts(t.w.conn, t.w.clock, c1.case_id, "kn-IN")[0].label == "ಫಾಲೋ-ಅಪ್ ನಿಗದಿಯಾಗಿದೆ"
