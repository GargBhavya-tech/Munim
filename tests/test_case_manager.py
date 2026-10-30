from core.audit.ledger import Ledger
from core.engine.case_manager import CaseManager
from core.engine.cases import CaseRepo
from core.engine.events import append_event
from packs.paytm_merchant.actions import build_registry

MID = "M-0001"


def mk(conn, clock):
    repo = CaseRepo(conn, clock, Ledger(conn, clock))
    return repo, CaseManager(repo, build_registry())


def opened(repo, pb, lane, state="ACTIVE", step=None, mid=MID):
    c = repo.open_case(playbook_id=pb, lane=lane, merchant_id=mid)
    c = repo.transition(c, "ACTIVE", step=step)
    return repo.transition(c, state) if state != "ACTIVE" else c


def touch(conn, clock, repo, case, eid):
    append_event(conn, event_id=eid, type="MERCHANT_MESSAGE", payload={}, ts_sim=clock.now_iso(), merchant_id=MID, case_id=case.case_id)
    repo.link_event(case.case_id, eid)


def test_open_c2_new_loan_message_keeps_focus_on_c2_and_parks_s3(conn, clock):
    repo, cm = mk(conn, clock)
    c2 = opened(repo, "C2", "P1", step="test_announcement")
    touch(conn, clock, repo, c2, "E-1")
    s3 = repo.open_case(playbook_id="S3", lane="P3", merchant_id=MID)       # the loan message just opened it
    touch(conn, clock, repo, s3, "E-2")                                      # ...and it is the MOST RECENT
    res = cm.focus(MID, [s3.case_id])
    assert res.focus.case_id == c2.case_id and res.focus.state == "ACTIVE"
    parked = repo.load(s3.case_id)
    assert parked.state == "PARKED"
    assert parked.resume_note == "I'll come back to S3 once C2 is sorted."
    assert "\n" not in parked.resume_note
    assert [p.case_id for p in res.parked] == [s3.case_id]


def test_focus_does_not_jump_to_p3_just_because_it_was_mentioned_last(conn, clock):
    repo, cm = mk(conn, clock)
    p1 = opened(repo, "C1", "P1")
    p3 = opened(repo, "S2", "P3")
    touch(conn, clock, repo, p1, "E-1")
    touch(conn, clock, repo, p3, "E-2")
    assert cm.focus(MID, [p3.case_id, p1.case_id]).focus.case_id == p1.case_id


def test_ties_go_to_most_recent_and_old_case_is_parked_with_resume_note(conn, clock):
    repo, cm = mk(conn, clock)
    c2 = opened(repo, "C2", "P1", step="replace_or_visit")
    c1 = opened(repo, "C1", "P1")
    touch(conn, clock, repo, c2, "E-1")
    touch(conn, clock, repo, c1, "E-2")             # merchant switched topic to the settlement
    res = cm.focus(MID, [c1.case_id])
    assert res.focus.case_id == c1.case_id
    old = repo.load(c2.case_id)
    assert old.state == "PARKED" and old.resume_note == "Resume C2 at step replace_or_visit after C1."


def test_lane_order_p0_beats_p1_beats_p2(conn, clock):
    repo, cm = mk(conn, clock)
    s1 = opened(repo, "S1", "P2")
    c1 = opened(repo, "C1", "P1")
    c10 = opened(repo, "C10", "P0")
    touch(conn, clock, repo, c10, "E-1")
    touch(conn, clock, repo, c1, "E-2")             # newer, but lower lane
    touch(conn, clock, repo, s1, "E-3")
    res = cm.focus(MID, [s1.case_id, c1.case_id, c10.case_id])
    assert res.focus.case_id == c10.case_id
    assert {repo.load(c.case_id).state for c in (s1, c1)} == {"PARKED"}


def test_parked_case_unparks_after_the_blocking_cases_close(conn, clock):
    repo, cm = mk(conn, clock)
    c2 = opened(repo, "C2", "P1")
    c1 = opened(repo, "C1", "P1")
    s3 = opened(repo, "S3", "P3")
    cm.park(s3, "later")
    assert cm.release_parked(MID) == []               # fix-first still holds S3 back
    repo.transition(c2, "RESOLVED")
    assert cm.release_parked(MID) == []               # C1 still open
    repo.transition(c1, "RESOLVED")
    back = cm.release_parked(MID)
    assert [c.case_id for c in back] == [s3.case_id] and repo.load(s3.case_id).state == "ACTIVE"


def test_focus_is_per_merchant(conn, clock):
    repo, cm = mk(conn, clock)
    a = opened(repo, "C1", "P1", mid="M-0001")
    b = opened(repo, "S2", "P3", mid="M-0002")
    assert cm.focus("M-0002", [b.case_id]).focus.case_id == b.case_id
    assert repo.load(a.case_id).state == "ACTIVE" and repo.load(b.case_id).state == "ACTIVE"
