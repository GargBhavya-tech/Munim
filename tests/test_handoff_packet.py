"""#54 handoff packet: schema, verified summary (every sentence cites existing events), rules by reason class, stored row."""
import json
from datetime import timedelta

import pytest

from core.actions.types import ActionIntent, Violation
from core.engine.clock import iso
from core.engine.handoff import (DEFAULT_CLASS, HandoffService, PacketInvalid, classify, ledger_summarizer, sentence_ok, validate_packet,
                                 verify_summary)
from tests.world import World

MID = "M-0006"


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock)


@pytest.fixture
def svc(world):
    return HandoffService(world.conn, world.clock, world.ledger)


@pytest.fixture
def case(world):
    """A real case with a real event, a real R2 action (a ticket) and a real escalation in its ledger."""
    c = world.engine.open_case(MID, "S1_ACTIVATION", language="kn-IN")
    world.inject("E-STALL", "SIGNUP_STALLED")
    world.engine.handle_event({"event_id": "E-STALL", "type": "SIGNUP_STALLED", "merchant_id": MID, "case_id": c.case_id})
    out = world.executor.submit(ActionIntent("raise_ticket", {"merchant_id": MID, "type": "SETTLEMENT", "system_object": "settlement",
                                                              "summary": "settlement held", "case_id": c.case_id}, case_id=c.case_id, step_seq=50, allowlist=("raise_ticket",)))
    assert out.status == "OK", out.error
    c = world.engine.escalate(world.engine.repo.load(c.case_id), ["SETTLEMENT_HELD_RISK", "AMOUNT_ABOVE_LIMIT"])
    return c


def make(svc, case, **kw):
    reasons = kw.pop("reasons", ["SETTLEMENT_HELD_RISK", "AMOUNT_ABOVE_LIMIT"])
    return svc.create_packet(case, reasons, kw.pop("violations", [Violation("RISK-HOLD-001", "Risk hold", "ESCALATE", "held", route="risk_reviewer"),
                                                                    Violation("MONEY-LIMIT-001", "Money", "ESCALATE", "big", route="ops_admin")]),
                             **kw)


# ------------------------------------------------------------------ the packet
def test_packet_is_complete_and_validates(svc, case, world):
    h = make(svc, case, facts={"language": "kn-IN", "amount_paise": 1_240_000, "done_fields": {"kyc": "APPROVED", "settlement": "HELD_RISK"}},
             sentiment_risk={"frustration": 0.7}, claims_blocked=[{"text": "It will reach your bank tomorrow.", "reason": "UNSUPPORTED"}])
    p = h.packet
    assert validate_packet(p) == []
    assert p["case_id"] == case.case_id and p["playbook"] == "S1_ACTIVATION" and p["lane"] == case.lane
    assert p["escalated_at"] == "SIM " + world.clock.now_iso()
    assert p["reasons"] == ["SETTLEMENT_HELD_RISK", "AMOUNT_ABOVE_LIMIT"]
    assert p["policy_rule_ids"] == ["MONEY-LIMIT-001", "RISK-HOLD-001"]
    assert p["facts"] == {"language": "kn-IN", "segment": p["facts"]["segment"], "amount_inr": 12400,
                          "done_fields": {"kyc": "APPROVED", "settlement": "HELD_RISK"}}
    assert p["facts"]["segment"] == world.conn.execute("SELECT segment FROM merchants WHERE merchant_id=?", (MID,)).fetchone()[0]
    assert p["sentiment_risk"] == {"frustration": 0.7, "fraud_flags": [], "regulatory_mentions": []}
    assert p["claims_blocked"] == [{"text": "It will reach your bank tomorrow.", "reason": "UNSUPPORTED"}]
    assert [a["action"] for a in p["actions_taken"]] == ["raise_ticket"] and p["actions_taken"][0]["id"].startswith("ACT-")
    assert len(p["open_compensations"]) == 1 and p["open_compensations"][0].startswith("withdraw_ticket(")


def test_packet_is_stored_in_the_handoffs_table_and_ledgered(svc, case, world):
    h = make(svc, case)
    row = world.conn.execute("SELECT * FROM handoffs WHERE handoff_id=?", (h.handoff_id,)).fetchone()
    assert (row["case_id"], row["status"], row["owner_role"], row["verdict"]) == (case.case_id, "OPEN", "risk_reviewer", None)
    assert json.loads(row["packet_json"]) == h.packet
    assert row["deadline"] == iso(world.clock.now() + timedelta(hours=2)) and h.packet["deadline"] == "SIM " + row["deadline"]
    esc = [e for e in world.ledger.entries(case.case_id) if e.kind == "ESCALATION" and "handoff_id" in e.payload]
    assert len(esc) == 1 and esc[0].payload["handoff_id"] == h.handoff_id


def test_every_summary_sentence_cites_existing_events_of_this_case(svc, case, world):
    p = make(svc, case).packet
    seqs = {e.seq: e for e in world.ledger.entries(case.case_id)}
    assert p["summary_sentences"], "a packet without a verified sentence is not a packet"
    for s in p["summary_sentences"]:
        assert s["refs"] and all(r in seqs and seqs[r].case_id == case.case_id for r in s["refs"])
        assert all(f"ev:{r}" in p["summary"] for r in s["refs"])
    assert p["summary"].count("[ev:") == len(p["summary_sentences"])
    cited = {r for s in p["summary_sentences"] for r in s["refs"]}
    assert any(seqs[r].kind == "ACTION_RESULT" and seqs[r].payload["action"] == "raise_ticket" for r in cited)


# ------------------------------------------------------------------ the checker (the Watch out: sentences without event references)
def entries_for(world, case):
    return {e.seq: e for e in world.ledger.entries(case.case_id)}


def first_seq(entries, kind, **match):
    return next(s for s, e in entries.items() if e.kind == kind and all(e.payload.get(k) == v for k, v in match.items()))


def test_checker_drops_a_sentence_with_no_reference(world, case):
    ents = entries_for(world, case)
    kept, dropped = verify_summary([{"text": "The merchant is upset.", "refs": []}, {"text": "No refs key at all."}], ents.values(), case.case_id)
    assert kept == [] and [d["why"] for d in dropped] == ["no event references", "no event references"]


def test_checker_drops_a_reference_to_an_event_that_does_not_exist(world, case):
    ents = entries_for(world, case)
    kept, dropped = verify_summary([{"text": "Something happened.", "refs": [max(ents) + 1000]}], ents.values(), case.case_id)
    assert kept == [] and "does not exist" in dropped[0]["why"]


def test_checker_drops_a_reference_to_another_cases_event(world, case):
    other = world.engine.open_case("M-0007", "S1_ACTIVATION")
    other_seq = world.ledger.entries(other.case_id)[0].seq
    kept, dropped = verify_summary([{"text": "Something happened.", "refs": [other_seq]}], world.ledger.entries(), case.case_id)
    assert kept == [] and "does not exist for this case" in dropped[0]["why"]


def test_checker_drops_a_sentence_whose_cited_event_lacks_the_action_or_status(world, case):
    ents = entries_for(world, case)
    ticket = first_seq(ents, "ACTION_RESULT", action="raise_ticket", status="OK")
    event = first_seq(ents, "EVENT")
    cases = [
        {"text": "A refund was issued.", "refs": [ticket], "expects": {"action": "create_refund_request"}},          # wrong action, explicit
        {"text": "Action create_refund_request ran.", "refs": [ticket]},                                             # wrong action, inferred from the text
        {"text": "The ticket result was FAILED.", "refs": [ticket], "expects": {"status": "FAILED"}},                # wrong status
        {"text": "The settlement was HELD_RISK.", "refs": [event]},                                                  # status token not in the cited event
        {"text": "Ticket raised.", "refs": [event], "expects": {"action": "raise_ticket"}},                          # cites an unrelated event
    ]
    kept, dropped = verify_summary(cases, ents.values(), case.case_id)
    assert kept == [] and len(dropped) == 5
    good, _ = verify_summary([{"text": "Action raise_ticket finished with status OK.", "refs": [ticket],
                               "expects": {"action": "raise_ticket", "status": "OK"}}], ents.values(), case.case_id)
    assert len(good) == 1


def test_an_injected_summarizer_cannot_smuggle_an_unverified_sentence_into_the_packet(svc, case, world):
    ents = entries_for(world, case)
    ticket = first_seq(ents, "ACTION_RESULT", action="raise_ticket", status="OK")

    def llm(c, entries, ctx):
        return [{"text": "Action raise_ticket finished with status OK.", "refs": [ticket], "expects": {"action": "raise_ticket"}},
                {"text": "The merchant was promised a refund tomorrow.", "refs": []},
                {"text": "A refund was issued.", "refs": [ticket], "expects": {"action": "create_refund_request"}}]

    h = make(svc, case, summarizer=llm)
    assert [s["text"] for s in h.packet["summary_sentences"]] == ["Action raise_ticket finished with status OK."]
    assert "refund" not in h.packet["summary"]
    assert validate_packet(h.packet) == []


def test_when_the_injected_summarizer_yields_nothing_verifiable_the_ledger_fallback_fills_in(svc, case):
    h = make(svc, case, summarizer=lambda c, e, ctx: [{"text": "Invented.", "refs": []}])
    assert h.packet["summary_sentences"] and "Invented" not in h.packet["summary"]


def test_fallback_summarizer_is_deterministic_and_everything_it_says_passes_the_checker(world, case):
    ents = list(world.ledger.entries(case.case_id))
    s1, s2 = ledger_summarizer(case, ents), ledger_summarizer(case, ents)
    assert s1 == s2 and s1
    by_seq = {e.seq: e for e in ents}
    assert all(sentence_ok(s, by_seq, case.case_id)[0] for s in s1)
    texts = " ".join(s["text"] for s in s1)
    assert "SIGNUP_STALLED" in texts and "raise_ticket" in texts and "ESCALATED" in texts
    assert "refund" not in texts.lower()


# ------------------------------------------------------------------ recommended next step and deadline: rules, not a model
@pytest.mark.parametrize("reasons,rules,role,hours,needle", [
    (["SETTLEMENT_HELD_RISK"], [], "risk_reviewer", 2, "Risk reviewer"),
    (["FRAUD_SIGNAL"], [], "risk_reviewer", 2, "hold or flag"),
    (["REGULATORY_MENTION"], [], "ops_admin", 2, "regulatory mention"),
    (["WANTS_HUMAN"], [], "support_executive", 2, "asked for a person"),
    (["NAME_MISMATCH"], [], "risk_reviewer", 4, "name"),
    (["AMOUNT_ABOVE_LIMIT"], [], "ops_admin", 4, "money limit"),
    (["OUT_OF_BAND"], ["WAIVER-BAND-001"], "sales_lead", 4, "waiver"),
    (["TOOL_FAILURE"], [], "ops_admin", 2, "failing system"),
    (["LOOP"], [], "support_executive", 4, "could not continue"),
    (["PLAYBOOK_ESCALATE_WHEN: kyc.attempts >= 2"], [], "support_executive", 4, "review the case"),
    (["SETTLEMENT_HELD_RISK", "AMOUNT_ABOVE_LIMIT"], ["RISK-HOLD-001", "MONEY-LIMIT-001"], "risk_reviewer", 2, "Risk reviewer"),
    (["AMOUNT_ABOVE_LIMIT", "WANTS_HUMAN"], [], "support_executive", 2, "asked for a person"),     # the more urgent class wins
])
def test_owner_deadline_and_step_come_from_reason_class(svc, case, world, reasons, rules, role, hours, needle):
    vs = [Violation(r, "t", "ESCALATE", "x") for r in rules]
    p = svc.create_packet(case, reasons, vs).packet
    assert p["owner_role"] == role
    assert p["deadline"] == "SIM " + iso(world.clock.now() + timedelta(hours=hours))
    assert needle in p["recommended_next_step"] and f"within {hours} hours" in p["recommended_next_step"]


def test_next_step_names_the_merchants_language(svc, case):
    assert "in Kannada within 2 hours" in make(svc, case).packet["recommended_next_step"]
    assert "in Hindi" in make(svc, case, facts={"language": "hi-IN"}).packet["recommended_next_step"]


def test_policy_route_picks_the_reviewer_for_price_and_money(svc, case):
    v = Violation("WAIVER-BAND-001", "Waiver", "ESCALATE", "x", route="area_manager")
    assert svc.create_packet(case, ["OUT_OF_BAND"], [v]).packet["owner_role"] == "area_manager"
    # but a risk reason keeps the risk reviewer even if a violation names someone else
    assert svc.create_packet(case, ["SETTLEMENT_HELD_RISK"], [v]).packet["owner_role"] == "risk_reviewer"


def test_classify_default_is_general():
    assert classify(["SOMETHING_NEW"]) is DEFAULT_CLASS


# ------------------------------------------------------------------ actions, compensations, other cases
def add_action(conn, case_id, action_id, name, comp_action, comp_args, until):
    conn.execute("INSERT INTO actions(action_id, case_id, name, cls, args_json, idempotency_key, status, step_seq, ts_sim) VALUES (?,?,?,?,?,?,?,?,?)",
                 (action_id, case_id, name, "R2", "{}", f"k-{action_id}", "OK", 90, "2026-10-31T09:00:00+05:30"))
    conn.execute("INSERT INTO compensations(action_id, compensation_json, compensable_until, status) VALUES (?,?,?, 'OPEN')",
                 (action_id, json.dumps({"action": comp_action, "args": comp_args, "of": name}), until))
    conn.commit()


def test_compensable_until_is_shown_and_an_expired_window_is_not_offered(svc, case, world):
    soon = iso(world.clock.now() + timedelta(hours=24))
    add_action(world.conn, case.case_id, "ACT-9001", "create_device_replacement", "cancel_device_replacement",
               {"merchant_id": MID, "replacement_id": "RPL-1"}, soon)
    add_action(world.conn, case.case_id, "ACT-9002", "apply_fee_waiver", "revoke_fee_waiver", {"waiver_id": "W-9"},
               iso(world.clock.now() - timedelta(hours=1)))
    p = make(svc, case).packet
    by_id = {a["id"]: a for a in p["actions_taken"]}
    assert by_id["ACT-9001"]["compensable_until"] == "SIM " + soon
    assert by_id["ACT-9002"]["action"] == "apply_fee_waiver"                          # still listed as taken
    assert "cancel_device_replacement(M-0006, RPL-1)" in p["open_compensations"]
    assert not any("revoke_fee_waiver" in c for c in p["open_compensations"])         # window closed: Undo would be refused
    open_ticket = [a for a in p["actions_taken"] if a["action"] == "raise_ticket"][0]
    assert open_ticket["compensable_until"] is None                                   # open-ended: no deadline to show


def test_other_open_cases_of_the_same_merchant_only(svc, case, world):
    other = world.engine.open_case(MID, "C2_SOUNDBOX_FAULT")
    world.engine.repo.transition(other, "PARKED", reason="fix-first")
    world.engine.open_case("M-0007", "C2_SOUNDBOX_FAULT")
    done = world.engine.open_case(MID, "C2_SOUNDBOX_FAULT")
    world.engine.repo.transition(done, "ACTIVE", reason="t")
    world.engine.repo.transition(world.engine.repo.load(done.case_id), "RESOLVED", reason="t")
    assert make(svc, case).packet["other_open_cases"] == ["C2_SOUNDBOX_FAULT (parked)"]


# ------------------------------------------------------------------ schema
def test_schema_rejects_an_incomplete_packet(svc, case):
    good = make(svc, case).packet
    for field in ("reasons", "summary", "deadline", "facts", "open_compensations", "owner_role", "recommended_next_step"):
        bad = {k: v for k, v in good.items() if k != field}
        assert any(field in e for e in validate_packet(bad)), field
    assert validate_packet({**good, "deadline": "tomorrow"})
    assert validate_packet({**good, "lane": "P9"})
    assert validate_packet({**good, "reasons": []})
    assert validate_packet({**good, "summary_sentences": [{"text": "x", "refs": []}]})
    assert validate_packet({**good, "owner_role": "wizard"})
    assert validate_packet({**good, "facts": {"language": "kn-IN"}})


def test_create_packet_refuses_to_store_an_invalid_packet(svc, world):
    c = world.engine.open_case(MID, "S1_ACTIVATION")
    n = world.conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]
    with pytest.raises(PacketInvalid):
        svc.create_packet(c, [], [])                 # no reasons
    assert world.conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == n
