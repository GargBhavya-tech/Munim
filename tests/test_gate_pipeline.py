"""#50 confidence tiers and the whole gate pipeline (verify -> render -> faithfulness -> compliance). Fakes for the model, NLI and translator."""
import json

import pytest

from core.audit.ledger import Ledger, verify_chain
from core.engine.escalation import HardTriggers
from core.engine.workflow import Decision as EngineDecision
from core.gate.faithfulness import FaithfulnessChecker
from core.gate.phrases import Phrases
from core.gate.pipeline import CLARIFY, ESCALATE, NO_REPLY, REWRITE, SEND, GroundingGate
from core.gate.render import Renderer
from core.gate.sources import Claim, SourceBook
from core.gate.tiers import TierResult, decide_tier, enforce, prior_blue_turns
from core.gate.verify import GroundingVerifier
from tests.fakes_gate import FakeNLI, FakeTranslator, llm

PH = Phrases()
TOOL, KB = "tool:get_settlements#call_1", "kb:settlement_timelines#pending@v1"
C1 = Claim("c1", "The amount is 8200 rupees.", TOOL)
C2 = Claim("c2", "The status is PENDING.", TOOL)
BAD = Claim("c3", "The settlement will arrive tomorrow.", TOOL)          # not in the source: the NLI says neutral
FAKE_FEE = Claim("c4", "The fee is 99 rupees.", TOOL)                     # an invented number


def book():
    b = SourceBook()
    b.add_tool_result(TOOL, {"settlement_id": "S-0100", "amount_paise": 820000, "status": "PENDING"})
    return b


def gate(replies, conn=None, clock=None, table=None, nli=None):
    ledger = Ledger(conn, clock) if conn is not None else None
    client, prov = llm(replies)
    nli = nli or FakeNLI()
    g = GroundingGate(GroundingVerifier(nli), Renderer(client, PH), FaithfulnessChecker(nli, FakeTranslator(table)), ledger=ledger)
    return g, prov, ledger


def sent(*items):
    return {"sentences": list(items)}


def cl(text, *ids):
    return {"claim_ids": list(ids), "text": text}


GOOD = sent(cl("The amount is 8200 rupees.", "c1"), cl("The status is PENDING.", "c2"))


# ================================================================== #50 tiers
@pytest.mark.parametrize("score,prior,ask,unver,outside,tier,reason", [
    (0.375, 0, False, False, False, "WHITE", None),
    (0.44, 0, False, False, False, "WHITE", None),
    (0.45, 0, False, False, False, "BLUE", None),
    (0.50, 0, False, False, False, "BLUE", None),
    (0.60, 0, False, False, False, "RED", "SOFT_SCORE"),
    (0.65, 0, False, False, False, "RED", "SOFT_SCORE"),
    (0.50, 1, False, False, False, "RED", "BLUE_REPEATED"),
    (0.10, 0, True, False, False, "BLUE", None),
    (0.10, 1, True, False, False, "RED", "BLUE_REPEATED"),
    (0.10, 0, False, True, False, "RED", "UNVERIFIABLE_CLAIM"),
    (0.10, 0, False, False, True, "RED", "OUTSIDE_KNOWLEDGE"),
])
def test_tier_table(score, prior, ask, unver, outside, tier, reason):
    t = decide_tier(score, prior_blue_turns=prior, ask_clarification=ask, claim_unverifiable=unver, outside_knowledge=outside)
    assert (t.tier, t.reason, t.escalate, t.asks_question) == (tier, reason, tier == "RED", tier == "BLUE")


def test_blue_and_red_cannot_carry_an_action_or_a_message_and_white_is_untouched():
    d = EngineDecision(action=("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}), message_en="x", follow_up_hours=2.0, channel="app")
    for tier in ("BLUE", "RED"):
        out = enforce(TierResult(tier), d)
        assert out.action is None and out.message_en is None and out.follow_up_hours is None
    assert enforce(TierResult("WHITE"), d) is d


def test_a_second_ambiguous_turn_in_a_row_escalates_using_the_ledger_history(conn, clock):
    g, _, ledger = gate([])
    ledger = Ledger(conn, clock)
    g = GroundingGate(GroundingVerifier(FakeNLI()), Renderer(llm([])[0], PH), FaithfulnessChecker(FakeNLI(), FakeTranslator()), ledger=ledger)
    first = g.run(claims=[], book=book(), language="en-IN", case_id="CS-1", tier=decide_tier(0.5, prior_blue_turns=prior_blue_turns(ledger.entries("CS-1"))))
    assert first.status == CLARIFY and prior_blue_turns(ledger.entries("CS-1")) == 1
    second_tier = decide_tier(0.5, prior_blue_turns=prior_blue_turns(ledger.entries("CS-1")))
    second = g.run(claims=[], book=book(), language="en-IN", case_id="CS-1", tier=second_tier)
    assert second.status == ESCALATE and second.reasons == ["BLUE_REPEATED"]
    ledger.append(kind="VERIFY", actor="SYSTEM", case_id="CS-1", payload={"status": "SEND", "tier": "WHITE"})
    assert prior_blue_turns(ledger.entries("CS-1")) == 0


def test_blue_means_zero_claims_rendered_zero_model_calls_and_one_question():
    g, prov, _ = gate([])
    out = g.run(claims=[C1, C2], book=book(), language="hinglish", tier=TierResult("BLUE"))
    assert out.status == CLARIFY and out.text == PH.text("clarify", "hinglish") and prov.seen == [] and out.verdicts == []


def test_red_hands_off_with_the_fixed_line_and_a_reason():
    g, prov, _ = gate([])
    out = g.run(claims=[C1], book=book(), language="kn-IN", tier=TierResult("RED", "OUTSIDE_KNOWLEDGE"))
    assert out.status == ESCALATE and out.text == PH.text("handoff", "kn-IN") and out.reasons == ["OUTSIDE_KNOWLEDGE"] and prov.seen == []


# ================================================================== pipeline
def test_a_verified_reply_is_sent_and_the_ledger_has_the_chips(conn, clock):
    g, prov, ledger = gate([GOOD], conn, clock)
    out = g.run(claims=[C1, C2], book=book(), language="en-IN", case_id="CS-1")
    assert out.status == SEND and out.text == "The amount is 8200 rupees. The status is PENDING." and out.sent_claim_ids == {"c1", "c2"} and out.attempts == 1
    v = [e for e in ledger.entries("CS-1") if e.kind == "VERIFY"][0]
    assert v.payload["status"] == "SEND" and v.payload["tier"] == "WHITE" and [c["level"] for c in v.payload["claims"]] == ["SUPPORTED", "SUPPORTED"]
    assert [s["ok"] for s in v.payload["sentences"]] == [True, True] and out.ledger_seq == v.seq
    assert verify_chain(conn).ok


def test_a_blocked_claim_is_removed_visible_with_its_reason_and_never_reaches_the_renderer(conn, clock):
    g, prov, ledger = gate([sent(cl("The amount is 8200 rupees.", "c1"))], conn, clock)
    out = g.run(claims=[C1, BAD, FAKE_FEE], book=book(), language="en-IN", case_id="CS-1")
    assert out.status == SEND and "tomorrow" not in out.text and "99" not in out.text
    assert [(b.claim_id, b.color) for b in out.blocked] == [("c3", "red"), ("c4", "red")] and all(b.reason for b in out.blocked)
    req = json.loads(prov.seen[0][1]["content"])
    assert [c["id"] for c in req["claims"]] == ["c1"]
    chips = [e for e in ledger.entries("CS-1") if e.kind == "VERIFY"][0].payload["claims"]
    assert {c["id"]: c["reason"] for c in chips if c["color"] == "red"}.keys() == {"c3", "c4"} and "numbers_not_in_source: 99" in chips[2]["reason"]


def test_when_no_claim_survives_the_turn_agent_gets_one_rewrite_then_it_escalates():
    g, prov, _ = gate([])
    first = g.run(claims=[BAD, FAKE_FEE], book=book(), language="en-IN")
    assert first.status == REWRITE and "No claim could be verified" in first.rewrite_feedback and "numbers_not_in_source: 99" in first.rewrite_feedback and prov.seen == []
    second = g.run(claims=[BAD, FAKE_FEE], book=book(), language="en-IN", rewrite_used=True)
    assert second.status == ESCALATE and second.text == PH.text("handoff", "en-IN") and second.reasons[0].startswith("CLAIMS_UNVERIFIED") and second.grounding_failures == 2


def test_an_added_reassurance_is_caught_and_the_second_render_goes_out_clean():
    dirty = sent(cl("The amount is 8200 rupees.", "c1"), cl("Do not worry, it will be fixed soon.", "c2"))
    g, prov, _ = gate([dirty, GOOD])
    out = g.run(claims=[C1, C2], book=book(), language="en-IN")
    assert out.status == SEND and out.attempts == 2 and out.faithfulness_failures == 1 and "worry" not in out.text
    retry = json.loads(prov.seen[1][1]["content"])
    assert "sentence 2" in retry["feedback"] and "not_entailed_by_any_verified_claim" in retry["feedback"] and "worry" not in retry["feedback"]


def test_two_unfaithful_renders_escalate_with_the_claims_kept_and_trip_the_hard_trigger():
    dirty = sent(cl("The amount is 8200 rupees.", "c1"), cl("It will reach tomorrow.", "c2"))
    g, _, _ = gate([dirty, dirty])
    out = g.run(claims=[C1, C2], book=book(), language="en-IN")
    assert out.status == ESCALATE and out.text == PH.text("handoff", "en-IN") and out.faithfulness_failures == 2 and len(out.verdicts) == 2
    assert out.reasons[0].startswith("FAITHFULNESS_FAILED_TWICE") and "tomorrow" not in out.text
    codes = [t.code for t in HardTriggers(money_limit_paise=1_000_000).check(None, {"payload": {}}, {"faithfulness_failures": out.faithfulness_failures})]
    assert codes == ["FAITHFULNESS_FAILED_TWICE"]


def test_a_reply_in_the_wrong_language_is_a_failure_not_a_send():
    g, _, _ = gate([GOOD, GOOD])
    out = g.run(claims=[C1, C2], book=book(), language="kn-IN")          # the renderer answered in English although Kannada was required
    assert out.status == ESCALATE and "kn-IN" in out.reasons[0]


def test_an_unrenderable_model_output_escalates_instead_of_sending_anything():
    g, _, _ = gate([{"reply": "hi"}, {"reply": "hi"}, {"reply": "hi"}, {"reply": "hi"}])
    out = g.run(claims=[C1], book=book(), language="en-IN")
    assert out.status == ESCALATE and out.text == PH.text("handoff", "en-IN")


def test_no_claims_and_nothing_required_to_say_means_no_reply():
    g, _, _ = gate([])
    assert g.run(claims=[], book=book(), language="en-IN").status == NO_REPLY


@pytest.mark.parametrize("lang", ["en-IN", "hi-IN", "hinglish", "kn-IN"])
def test_first_contact_has_identity_and_the_pin_otp_line_and_an_ai_question_gets_a_truthful_yes(lang):
    g, _, _ = gate([])
    first = g.run(claims=[], book=book(), language=lang, first_contact=True)
    assert first.status == SEND and first.text == f"{PH.text('identity', lang)} {PH.text('pin_otp', lang)}"
    ai = g.run(claims=[], book=book(), language=lang, merchant_message="are you an AI?" if lang == "en-IN" else "क्या आप एआई हैं" if lang == "hi-IN"
               else "kya aap robot ho" if lang == "hinglish" else "ನೀವು ಎಐ ಆ")
    assert ai.status == SEND and ai.text == PH.text("ai_yes", lang)


def test_compliance_lines_come_before_a_verified_reply_on_first_contact():
    g, _, _ = gate([GOOD])
    out = g.run(claims=[C1, C2], book=book(), language="en-IN", first_contact=True)
    assert out.text.startswith("I am Munim, Paytm's AI assistant. Paytm never asks for your PIN or OTP. The amount is 8200 rupees.")
    assert [s.kind for s in out.sentences] == ["compliance", "compliance", "claim", "claim"]


def test_the_ledger_reply_text_is_what_was_said_and_compliance_lines_are_marked_ok(conn, clock):
    g, _, ledger = gate([GOOD], conn, clock)
    out = g.run(claims=[C1, C2], book=book(), language="en-IN", case_id="CS-2", first_contact=True)
    sents = [e for e in ledger.entries("CS-2") if e.kind == "VERIFY"][0].payload["sentences"]
    assert " ".join(s["text"] for s in sents) == out.text and all(s["ok"] for s in sents) and [s["kind"] for s in sents][:2] == ["compliance", "compliance"]
