from datetime import datetime, timedelta

import pytest

from core.audit.ledger import Ledger
from core.engine.cases import CaseRepo
from core.engine.clock import IST
from core.engine.contact_budget import CHANNELS, ContactBudget
from core.engine.scripted import scripted_decider
from core.engine.timers import TimerService
from tests.test_workflow import MID, PB, World, build, ev

NUDGE = {("N", "nudge"): {"message_en": "Please complete KYC.", "channel": "whatsapp"}}


def attempt(cb, mid, case, channel="whatsapp"):
    ok, why = cb.check(mid, channel, proactive=True, case_id=case)
    if ok:
        cb.record(mid, channel, case_id=case)
    return ok, why


def test_three_open_cases_each_wanting_a_nudge_yield_one_message_that_week(conn, clock):
    cb = ContactBudget(conn, clock)
    results = [attempt(cb, MID, c) for c in ("CS-1", "CS-2", "CS-3")]
    assert results[0] == (True, None) and [r[0] for r in results] == [True, False, False]
    assert conn.execute("SELECT COUNT(*) FROM contact_touches WHERE subject_id=?", (MID,)).fetchone()[0] == 1
    # the rest of the week, every case keeps asking every day: only ONE more touch (cap 2/week) gets through
    sent = 1
    for day in range(1, 7):
        clock.advance(timedelta(days=1))
        for c in ("CS-1", "CS-2", "CS-3"):
            sent += attempt(cb, MID, c)[0]
    assert sent == 2


def test_weekly_cap_counts_per_merchant_not_per_case(conn, clock):
    """Fails if touches are counted per case: each of the three touches below is on a DIFFERENT case."""
    cb = ContactBudget(conn, clock)
    assert attempt(cb, MID, "CS-1")[0]
    clock.advance(timedelta(hours=25))
    assert attempt(cb, MID, "CS-2")[0]
    clock.advance(timedelta(hours=25))
    ok, why = cb.check(MID, "whatsapp", proactive=True, case_id="CS-3")
    assert (ok, why) == (False, "WEEKLY_CAP")
    # another merchant is unaffected
    assert cb.check("M-0002", "whatsapp", proactive=True, case_id="CS-9") == (True, None)
    # the rolling window frees a slot once the first touch is 7 days old
    clock.advance(timedelta(days=5))   # now day 0 + 25h + 25h + 5d = first touch is > 7d old
    assert cb.check(MID, "whatsapp", proactive=True, case_id="CS-3") == (True, None)


def test_24h_gap_between_touches(conn, clock):
    cb = ContactBudget(conn, clock)
    attempt(cb, MID, "CS-1")
    clock.advance(timedelta(hours=11))
    assert cb.check(MID, "sms", proactive=True, case_id="CS-2") == (False, "COOLING_OFF")   # a different channel/case still waits
    clock.advance(timedelta(hours=13))
    assert cb.check(MID, "sms", proactive=True, case_id="CS-2") == (True, None)


def test_three_per_channel_per_case(conn, clock):
    cb = ContactBudget(conn, clock)
    for day in (31, 31 + 8, 31 + 16):                 # three weeks apart enough so weekly/gap never bite
        clock.set(datetime(2026, 10, 1, 9, tzinfo=IST) + timedelta(days=day - 1))
        assert attempt(cb, MID, "CS-1")[0]
    clock.advance(timedelta(days=8))
    assert cb.check(MID, "whatsapp", proactive=True, case_id="CS-1") == (False, "CHANNEL_CAP")
    assert cb.check(MID, "sms", proactive=True, case_id="CS-1") == (True, None)          # other channel
    assert cb.check(MID, "whatsapp", proactive=True, case_id="CS-2") == (True, None)     # other case


def test_quiet_hours_2100_to_0900(conn, clock):
    cb = ContactBudget(conn, clock)
    for (h, m), expect in [((8, 59), (False, "QUIET_HOURS")), ((9, 0), (True, None)), ((20, 59), (True, None)),
                           ((21, 0), (False, "QUIET_HOURS")), ((23, 30), (False, "QUIET_HOURS")), ((2, 0), (False, "QUIET_HOURS"))]:
        clock.set(datetime(2026, 11, 3, h, m, tzinfo=IST))
        assert cb.check(MID, "whatsapp", proactive=True) == expect, (h, m)


def test_stated_window_applies_only_inside_it(conn, clock):
    assert dict(conn.execute("SELECT contact_window_start s, contact_window_end e FROM merchants WHERE merchant_id='M-0003'").fetchone()) == {"s": "15:00", "e": "17:00"}
    cb = ContactBudget(conn, clock)
    for (h, m), expect in [((10, 0), (False, "OUTSIDE_STATED_WINDOW")), ((14, 59), (False, "OUTSIDE_STATED_WINDOW")), ((15, 0), (True, None)),
                           ((16, 59), (True, None)), ((17, 0), (False, "OUTSIDE_STATED_WINDOW")), ((22, 0), (False, "OUTSIDE_STATED_WINDOW"))]:
        clock.set(datetime(2026, 11, 3, h, m, tzinfo=IST))
        assert cb.check("M-0003", "whatsapp", proactive=True) == expect, (h, m)
    clock.set(datetime(2026, 11, 3, 10, 0, tzinfo=IST))
    assert cb.check("M-0001", "whatsapp", proactive=True) == (True, None)      # no window: the default hours apply


def test_merchant_initiated_is_always_allowed(conn, clock):
    cb = ContactBudget(conn, clock)
    attempt(cb, MID, "CS-1")
    clock.advance(timedelta(hours=1))
    assert cb.check(MID, "whatsapp", proactive=True)[0] is False       # capped / cooling off
    clock.set(datetime(2026, 10, 31, 23, 0, tzinfo=IST))                # quiet hours
    assert cb.check(MID, "whatsapp", proactive=False) == (True, None)
    cb.record_stop(MID)                                                 # even after a hard stop
    assert cb.check(MID, "whatsapp", proactive=False) == (True, None)
    # replying does not eat the proactive budget
    cb.record("M-0002", "whatsapp", case_id="CS-5", direction="OUT", proactive=False)
    cb.record("M-0002", "whatsapp", case_id="CS-5", direction="IN")
    clock.set(datetime(2026, 11, 3, 10, 0, tzinfo=IST))
    assert cb.check("M-0002", "whatsapp", proactive=True) == (True, None)


def test_stop_revokes_all_channels_blocks_proactive_and_cancels_pending_timers(conn, clock):
    ledger = Ledger(conn, clock)
    repo = CaseRepo(conn, clock, ledger)
    fired = []
    timers = TimerService(conn, clock, ledger, handler=fired.append)
    cb = ContactBudget(conn, clock, timers)
    mine = [repo.transition(repo.open_case(playbook_id="S1", lane="P2", merchant_id=MID), "ACTIVE") for _ in range(2)]
    other = repo.transition(repo.open_case(playbook_id="S1", lane="P2", merchant_id="M-0002"), "ACTIVE")
    for c in mine + [other]:
        timers.schedule(c.case_id, clock.now() + timedelta(hours=30), "follow_up")
    assert cb.record_stop(MID) == 2
    rows = conn.execute("SELECT channel, status FROM consent WHERE subject_id=?", (MID,)).fetchall()
    assert sorted(r["channel"] for r in rows) == sorted(CHANNELS) and {r["status"] for r in rows} == {"REVOKED"}
    clock.advance(timedelta(days=30))
    assert [e["case_id"] for e in fired] == [other.case_id]           # only the other merchant's follow-up fired
    for ch in CHANNELS:
        assert cb.check(MID, ch, proactive=True) == (False, "STOPPED")
    assert cb.check("M-0002", "whatsapp", proactive=True) == (True, None)


def test_engine_sends_exactly_one_proactive_message_for_three_cases(conn, clock):
    cb = ContactBudget(conn, clock)
    pbs = {"N": PB("N", "P1", ["nudge"], [])}
    eng, ex, world, ledger, _ = build(conn, clock, playbooks=pbs, decider=scripted_decider(NUDGE), contact_budget=cb)
    ex.completes = False   # the world is shared by the three cases; a nudge does not finish the step
    cases = [eng.open_case(MID, "N") for _ in range(3)]
    for i, c in enumerate(cases):
        eng.handle_event(ev(f"T-{i}", "TIMER_FIRED", case=c.case_id, kind="nudge"))
    sends = [i for i in ex.executed if i.action == "send_message"]
    assert len(sends) == 1 and sends[0].proactive is True and sends[0].args["body"] == "Please complete KYC."
    denied = [e.payload for c in cases for e in ledger.entries(c.case_id) if e.kind == "POLICY"]
    assert [d["reason"] for d in denied] == ["COOLING_OFF", "COOLING_OFF"] and denied[0]["rule_ids"] == ["CONTACT-BUDGET"]
    assert conn.execute("SELECT COUNT(*) FROM contact_touches WHERE direction='OUT'").fetchone()[0] == 1


def test_engine_zero_proactive_sends_after_stop_but_replies_still_go(conn, clock):
    cb = ContactBudget(conn, clock)
    pbs = {"N": PB("N", "P1", ["nudge"], [])}
    eng, ex, world, ledger, _ = build(conn, clock, playbooks=pbs, decider=scripted_decider(NUDGE), contact_budget=cb)
    cb.record_stop(MID)
    case = eng.open_case(MID, "N")
    for day in range(1, 6):
        clock.advance(timedelta(days=1))
        eng.handle_event(ev(f"T-{day}", "TIMER_FIRED", case=case.case_id, kind="nudge"))
    assert ex.executed == []
    eng.handle_event(ev("M-1", "MERCHANT_MESSAGE", case=case.case_id, text="kya hua?"))   # the merchant wrote: answered
    assert [i.action for i in ex.executed] == ["send_message"] and ex.executed[0].proactive is False
