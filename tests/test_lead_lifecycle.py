"""#101 lead lifecycle: a validated state machine; WON opens S1 and the lead stays open until S1 resolves (first settlement); only deduped leads start."""
import itertools
from datetime import timedelta

import pytest

from core.engine.scripted import scripted_decider
from jobs.dedupe import DedupeJob, outreach_ready
from jobs.lead_lifecycle import STATES, TRANSITIONS, IllegalTransition, LeadLifecycle, NotCleared
from tests.test_scenarios_s1_c2 import SCRIPT
from tests.world import World

MID = "M-0006"


@pytest.fixture
def w(db_path, clock):
    w = World(db_path, clock, None, mid=MID)
    w.engine.decider = scripted_decider(SCRIPT)
    DedupeJob(w.conn, clock, w.ledger).run()
    return w


@pytest.fixture
def life(w):
    return LeadLifecycle(w.conn, w.clock, w.ledger, w.engine, w.timers)


def cleared_lead(w, n=0) -> str:
    return outreach_ready(w.conn)[n]["lead_id"]


def to_qualified(life, lid):
    for s in ("RESEARCHED", "CONTACTED", "ENGAGED", "QUALIFIED"):
        life.move(lid, s)


def test_every_state_pair_is_accepted_exactly_when_the_table_allows_it(w, life):
    """Brute force over all 12 x 12 pairs: a move succeeds iff TRANSITIONS says so (the state is forced into place for each trial)."""
    lid = cleared_lead(w)
    for a, b in itertools.product(STATES, STATES):
        w.conn.execute("UPDATE leads SET state = ?, matched_merchant_id = NULL WHERE lead_id = ?", (a, lid))
        if a == "NEW" and b == "RESEARCHED":
            continue                                                    # covered by the dedupe-gate tests below
        if b in TRANSITIONS[a]:
            assert life.move(lid, b).state == b, (a, b)
        else:
            with pytest.raises(IllegalTransition):
                life.move(lid, b)
            assert w.conn.execute("SELECT state FROM leads WHERE lead_id = ?", (lid,)).fetchone()[0] == a       # an illegal move changes nothing


def test_the_happy_path_and_the_retry_loops(w, life):
    a, b = cleared_lead(w, 0), cleared_lead(w, 1)
    to_qualified(life, a)
    assert life.history(a) == [("NEW", "RESEARCHED"), ("RESEARCHED", "CONTACTED"), ("CONTACTED", "ENGAGED"), ("ENGAGED", "QUALIFIED")]
    for s in ("RESEARCHED", "CONTACTED", "NO_REPLY", "NURTURE", "CONTACTED", "NOT_NOW", "NURTURE"):
        life.move(b, s)
    assert life.get(b).state == "NURTURE" and life.get(b).open


def test_only_a_lead_cleared_by_duplicate_matching_may_start(w, life):
    existing = w.conn.execute("SELECT lead_id FROM leads WHERE state = 'EXISTING_MERCHANT' LIMIT 1").fetchone()[0]
    w.conn.execute("UPDATE leads SET state = 'NEW' WHERE lead_id = ?", (existing,))       # even forced back to NEW, the merchant match still blocks it
    with pytest.raises(NotCleared):
        life.move(existing, "RESEARCHED")
    w.conn.execute("INSERT INTO leads(lead_id, name, state, confidence, provenance_json) VALUES ('L-9100', 'Never Checked Traders', 'NEW', 0.9, '{}')")
    with pytest.raises(NotCleared):
        life.move("L-9100", "RESEARCHED")                                        # never run through dedupe
    assert life.move(cleared_lead(w), "RESEARCHED").state == "RESEARCHED"


def test_won_opens_s1_and_the_lead_stays_open_until_the_first_settlement(w, life):
    lid = cleared_lead(w)
    to_qualified(life, lid)
    lead = life.win(lid, MID, language="kn-IN")
    assert lead.state == "WON" and lead.status == "WON_PENDING_FIRST_SETTLEMENT" and lead.open
    case = life.s1_case(lid)
    assert (case.playbook_id, case.lead_id, case.merchant_id, case.language) == ("S1_ACTIVATION", lid, MID, "kn-IN")
    assert w.conn.execute("SELECT matched_merchant_id FROM leads WHERE lead_id = ?", (lid,)).fetchone()[0] == MID
    # activation proceeds on the simulated clock; WON never closes the lead, only the first settlement does
    w.inject("W-1", "SIGNUP_STALLED"); w.dispatcher.run()
    w.clock.advance(timedelta(hours=2)); w.control("/mock/control/kyc/{mid}", status="APPROVED")
    w.inject("W-2", "KYC_SUBMITTED"); w.dispatcher.run()
    assert life.get(lid).open and life.get(lid).status == "WON_PENDING_FIRST_SETTLEMENT"
    w.clock.advance(timedelta(hours=24)); w.control("/mock/control/device/{mid}", status="ONLINE")
    w.inject("W-3", "DEVICE_DELIVERED"); w.dispatcher.run()
    w.pay("W-pay", 24000); w.dispatcher.run()
    assert life.get(lid).open                                                           # paid, not yet settled
    sid = w.conn.execute("SELECT settlement_id FROM settlements WHERE merchant_id = ? AND status = 'PENDING' ORDER BY expected_at DESC", (MID,)).fetchone()[0]
    w.advance_settlement(sid, "W-adv"); w.dispatcher.run()
    assert life.s1_case(lid).state == "RESOLVED"
    done = life.get(lid)
    assert done.status == "ACTIVATED" and done.open is False and done.state == "WON"


def test_only_a_qualified_or_handed_off_lead_can_be_won(w, life):
    lid = cleared_lead(w)
    life.move(lid, "RESEARCHED")
    with pytest.raises(IllegalTransition):
        life.win(lid, MID)
    assert w.conn.execute("SELECT COUNT(*) FROM cases WHERE lead_id = ?", (lid,)).fetchone()[0] == 0        # no S1 case from a refused win
    h = cleared_lead(w, 1)
    to_qualified(life, h)
    life.move(h, "HANDED_OFF")
    assert life.win(h, "M-0005").state == "WON"


def test_do_not_contact_is_final_blocks_the_number_and_cancels_timers(w, life):
    lid = cleared_lead(w)
    phone = w.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (lid,)).fetchone()[0]
    to_qualified(life, lid)
    life.win(lid, MID)
    case = life.s1_case(lid)
    w.timers.schedule(case.case_id, w.clock.now() + timedelta(hours=5), "follow_up", {})
    assert w.timers.pending(case.case_id)
    life.move(lid, "DO_NOT_CONTACT", "said stop")
    assert w.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (phone,)).fetchone() or phone is None
    assert w.timers.pending(case.case_id) == []
    for s in STATES:
        with pytest.raises(IllegalTransition):
            life.move(lid, s)


def test_every_transition_is_ledgered(w, life):
    lid = cleared_lead(w)
    life.move(lid, "RESEARCHED", "brief written")
    e = [x for x in w.ledger.entries() if x.kind == "STATE" and x.payload.get("lead_id") == lid][-1]
    assert (e.payload["from"], e.payload["state"], e.payload["reason"]) == ("NEW", "RESEARCHED", "brief written")
