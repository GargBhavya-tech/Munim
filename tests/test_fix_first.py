import pytest

from core.audit.ledger import Ledger
from core.engine.case_manager import CaseManager, allowed_actions, fix_first_active, intent_allowed
from core.engine.cases import CaseRepo
from core.engine.scripted import scripted_decider
from packs.paytm_merchant.actions import build_registry
from tests.test_workflow import MID, PB, FakeExecutor, World, build, ev

REG = build_registry()
OFFERS = REG.offer_actions()
# S2..S5-style playbooks: each lists its offer action(s) plus ordinary ones
S_PBS = {
    "S2": PB("S2", "P3", ["offer"], ["create_device_order", "apply_fee_waiver", "schedule_follow_up", "get_device_status"]),
    "S3": PB("S3", "P3", ["offer"], ["check_partner_eligibility", "send_partner_application_link", "schedule_follow_up"]),
    "S4": PB("S4", "P3", ["offer"], ["create_plan_change", "schedule_follow_up", "get_plan"]),
    "S5": PB("S5", "P3", ["offer"], ["apply_fee_waiver", "book_visit_or_call", "schedule_follow_up"]),
}


def test_registry_has_offer_actions_to_remove():
    assert {"create_device_order", "apply_fee_waiver", "create_plan_change", "send_partner_application_link"} <= OFFERS


def mkcases(conn, clock, lanes):
    repo = CaseRepo(conn, clock, Ledger(conn, clock))
    out = []
    for i, lane in enumerate(lanes):
        out.append(repo.transition(repo.open_case(playbook_id=f"X{i}", lane=lane, merchant_id=MID), "ACTIVE"))
    return repo, out


@pytest.mark.parametrize("pid", sorted(S_PBS))
def test_with_open_c1_no_offer_action_is_in_any_allowlist(conn, clock, pid):
    repo, cases = mkcases(conn, clock, ["P1", "P3"])
    pb = S_PBS[pid]
    al = allowed_actions(cases[1], pb, cases, REG)
    assert not (set(al) & OFFERS)
    assert "schedule_follow_up" in al                 # non-offer actions survive
    # sanity: the same call WITHOUT an open service case does contain the offer action (the test can fail)
    clean = allowed_actions(cases[1], pb, [cases[1]], REG)
    assert set(clean) & OFFERS


def test_p0_also_triggers_fix_first(conn, clock):
    repo, cases = mkcases(conn, clock, ["P0", "P3"])
    assert not set(allowed_actions(cases[1], S_PBS["S3"], cases, REG)) & OFFERS


def test_a_resolved_p1_no_longer_triggers_fix_first(conn, clock):
    repo, cases = mkcases(conn, clock, ["P1", "P3"])
    repo.transition(cases[0], "RESOLVED")
    assert not fix_first_active(repo.open_cases(MID))
    assert set(allowed_actions(cases[1], S_PBS["S3"], repo.open_cases(MID), REG)) & OFFERS


def test_suppress_proactive_sales(conn, clock):
    repo, cases = mkcases(conn, clock, ["P3"])
    cm = CaseManager(repo, REG)
    assert cm.suppress_proactive_sales(MID) is False
    c1 = repo.transition(repo.open_case(playbook_id="C1", lane="P1", merchant_id=MID), "ACTIVE")
    assert cm.suppress_proactive_sales(MID) is True
    assert cm.suppress_proactive_sales("M-0002") is False       # per merchant
    c1 = repo.transition(c1, "ESCALATED")
    assert cm.suppress_proactive_sales(MID) is True             # a human holds it: still open
    repo.transition(repo.transition(c1, "RETURNED_TO_AGENT"), "ACTIVE")
    c1 = repo.load(c1.case_id)
    repo.transition(c1, "RESOLVED")
    assert cm.suppress_proactive_sales(MID) is False


def test_intent_allowed_is_independent_of_the_allowlist():
    """Defence in depth: even if an offer action sneaks into the allowlist it is rejected under fix-first."""
    v = intent_allowed("send_partner_application_link", ["send_partner_application_link"], ["P1", "P3"], REG, case_lane="P3")
    assert not v.ok and v.rule_id == "FIX-FIRST-001"
    assert intent_allowed("send_partner_application_link", ["send_partner_application_link"], ["P3"], REG, case_lane="P3").ok
    assert not intent_allowed("schedule_follow_up", ["get_plan"], [], REG).ok            # not on the allowlist
    assert not intent_allowed("approve_kyc", ["approve_kyc"], [], REG).ok                # unknown/forbidden never allowed
    v = intent_allowed("send_message", ["send_message"], ["P1"], REG, case_lane="P3", proactive=True)
    assert not v.ok and v.rule_id == "FIX-FIRST-001"                                     # proactive sales outreach suppressed
    assert intent_allowed("send_message", ["send_message"], ["P1"], REG, case_lane="P1", proactive=True).ok


def test_engine_rejects_proposed_loan_link_before_the_executor(conn, clock):
    table = {("S3", "offer"): ("send_partner_application_link", {"merchant_id": MID})}
    eng, ex, world, ledger, _ = build(conn, clock, playbooks={"C1": PB("C1", "P1", ["fix"], ["raise_ticket"]), "S3": S_PBS["S3"]},
                                      decider=scripted_decider(table))
    c1 = eng.open_case(MID, "C1")
    eng.repo.transition(c1, "ACTIVE")
    s3 = eng.open_case(MID, "S3")
    r = eng.handle_event(ev("E-1", "MERCHANT_MESSAGE", case=s3.case_id, text="loan chahiye"))
    assert ex.calls == [] and ex.executed == []                       # zero calls reached the executor
    assert r.state == "PARKED"
    assert eng.repo.load(s3.case_id).resume_note.startswith("I'll come back to S3")
    pol = [e.payload for e in ledger.entries(s3.case_id) if e.kind == "POLICY"]
    assert pol == [{"action": "send_partner_application_link", "allow": False, "rule_ids": ["FIX-FIRST-001"],
                    "reason": "send_partner_application_link is an offer action and a P0/P1 case is open", "step": "offer"}]


def test_same_proposal_goes_through_when_no_service_case_is_open(conn, clock):
    table = {("S3", "offer"): ("send_partner_application_link", {"merchant_id": MID})}
    eng, ex, world, ledger, _ = build(conn, clock, playbooks={"S3": S_PBS["S3"]}, decider=scripted_decider(table))
    s3 = eng.open_case(MID, "S3")
    eng.handle_event(ev("E-1", "MERCHANT_MESSAGE", case=s3.case_id))
    assert [i.action for i in ex.executed] == ["send_partner_application_link"]


def test_offer_not_in_ctx_allowlist_given_to_the_decider(conn, clock):
    seen = []

    def decider(ctx):
        seen.append(ctx.allowlist)
        return scripted_decider({})(ctx)
    eng, ex, *_ = build(conn, clock, playbooks={"C1": PB("C1", "P1", ["fix"], ["raise_ticket"]), "S2": S_PBS["S2"]}, decider=decider)
    eng.repo.transition(eng.open_case(MID, "C1"), "ACTIVE")
    s2 = eng.open_case(MID, "S2")
    eng.handle_event(ev("E-1", "MERCHANT_MESSAGE", case=s2.case_id))
    assert seen and not (set(seen[0]) & OFFERS) and "schedule_follow_up" in seen[0]
