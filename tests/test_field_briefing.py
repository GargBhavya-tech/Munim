"""#110 briefings: depth by experience x value; every bullet is a verified claim with a real source; the grounding gate decides what is delivered."""
from core.gate.faithfulness import FaithfulnessChecker
from core.gate.phrases import Phrases
from core.gate.pipeline import ESCALATE, REWRITE, SEND, GateOutcome, GroundingGate
from core.gate.render import Renderer
from core.gate.sources import Claim, check_claims
from core.gate.verify import GroundingVerifier
from field.briefing import DETAILED, SHORT, STANDARD, answer_followup, brief_language, build_brief, deliver, depth_for, run_gate
from field.profiles import RepProfile
from tests.fakes_gate import FakeNLI, FakeTranslator, llm

LEAD = {"lead_id": "L-0007", "name": "Sri Lakshmi Stores", "category": "kirana", "value_paise": 6_000_000, "address": "12 Main Road", "best_time": "10:30 in the morning",
        "documents": "the shop licence"}
VISITS = {"last_outcome": "RESCHEDULED", "objection": "monthly fee"}
STOCK = {"product": "soundbox", "count": 3}


def rep(months, langs=None, rid="R-X"):
    return RepProfile(rid, "Rep", (12.9, 77.6), (600, 1140), langs or {"en-IN": "native", "kn-IN": "fluent"}, months, {}, ("soundbox",), {"soundbox": 3})


def real_gate(replies):
    client, _ = llm(replies)
    nli = FakeNLI()
    return GroundingGate(GroundingVerifier(nli), Renderer(client, Phrases()), FaithfulnessChecker(nli, FakeTranslator()))


class Recorder:
    def __init__(self):
        self.intents = []

    def __call__(self, intent):
        self.intents.append(intent)
        return "submitted"


# ---------------------------------------------------------------- depth
def test_depth_table():
    assert depth_for(4, 6_000_000) == DETAILED          # new rep, high value
    assert depth_for(40, 100_000) == SHORT              # veteran, low value
    assert depth_for(40, 6_000_000) == STANDARD         # veteran on a big lead keeps the normal brief
    assert depth_for(4, 100_000) == STANDARD
    assert depth_for(12, 4_999_999) == STANDARD and depth_for(11, 5_000_000) == DETAILED and depth_for(24, 4_999_999) == SHORT


def test_new_rep_high_value_gets_every_bullet_and_veteran_low_value_gets_three():
    new = build_brief(rep(4), LEAD, VISITS, STOCK)
    assert new.depth == DETAILED and new.fields == ["who", "value", "last_visit", "objection", "product_stock", "address", "best_time", "documents"]
    vet = build_brief(rep(40), {**LEAD, "value_paise": 100_000}, VISITS, STOCK)
    assert vet.depth == SHORT and vet.fields == ["who", "value", "last_visit"] and len(vet.claims) == 3
    assert [c.text for c in vet.claims] == ["The lead is Sri Lakshmi Stores, a kirana shop.", "The lead value is 1000 rupees.",
                                            "The last visit outcome was RESCHEDULED."]


# ---------------------------------------------------------------- Watch out: claims without sources
def test_every_bullet_cites_a_source_that_exists_and_its_numbers_match_it():
    b = build_brief(rep(4), LEAD, VISITS, STOCK)
    assert all(b.book.exists(c.source) for c in b.claims)
    checks = check_claims(b.claims, b.book)
    assert all(c.ok for c in checks) and len(checks) == 8
    assert "60000 rupees" in next(c.text for c in b.claims if c.id == "b_value")          # 6,000,000 paise shown as rupees by code, from the read


def test_a_fact_missing_from_the_system_read_is_dropped_never_made_up():
    b = build_brief(rep(4), {k: v for k, v in LEAD.items() if k not in ("best_time", "documents")}, {"last_outcome": "RESCHEDULED"}, STOCK)
    assert b.dropped == ["objection", "best_time", "documents"] and "best_time" not in b.fields
    assert not any("objection" in c.text or "best time" in c.text for c in b.claims)
    assert len(b.claims) == 5 and all(c.ok for c in check_claims(b.claims, b.book))


def test_the_checker_rejects_a_bullet_with_a_number_the_source_does_not_hold():
    b = build_brief(rep(4), LEAD, VISITS, STOCK)
    forged = Claim("b_value", "The lead value is 90000 rupees.", b.claims[1].source)
    bad = check_claims([forged], b.book)[0]
    assert not bad.ok and bad.unmatched_numbers
    ghost = Claim("b_x", "The lead has 3 stores.", "tool:get_lead#NOPE")
    assert not check_claims([ghost], b.book)[0].source_exists


# ---------------------------------------------------------------- language, gate, delivery
def test_the_brief_is_in_the_persons_native_regional_language():
    assert brief_language(rep(30, {"kn-IN": "native", "hi-IN": "fluent", "en-IN": "basic"})) == "kn-IN"
    assert brief_language(rep(30, {"hi-IN": "native", "en-IN": "fluent"})) == "hi-IN"
    assert brief_language(rep(30, {"en-IN": "native"})) == "en-IN"

    seen = {}

    class Gate:
        def run(self, **kw):
            seen.update(kw)
            return GateOutcome(SEND, "x")

    b = build_brief(rep(30, {"kn-IN": "native"}), LEAD, VISITS, STOCK)
    run_gate(Gate(), b)
    assert seen["language"] == "kn-IN" and seen["claims"] is b.claims and seen["book"] is b.book


def test_a_brief_goes_through_the_real_gate_and_is_delivered_as_an_executor_intent():
    b = build_brief(rep(40), {**LEAD, "value_paise": 100_000}, VISITS, STOCK)
    sentences = [{"claim_ids": [c.id], "text": c.text} for c in b.claims]
    out = run_gate(real_gate([{"sentences": sentences}]), b, case_id="CASE-1")
    assert out.status == SEND and out.sent_claim_ids == {"b_who", "b_value", "b_last_visit"}
    rec = Recorder()
    assert deliver(out, b, rec) == "submitted"
    (intent,) = rec.intents
    assert intent.action == "notify_field_person" and intent.allowlist == ("notify_field_person",) and intent.args["person_id"] == "R-X"
    assert intent.args["body"] == out.text and "1000 rupees" in out.text and intent.step_seq == 0


def test_an_invented_number_in_the_rendered_text_blocks_delivery():
    b = build_brief(rep(40), {**LEAD, "value_paise": 100_000}, VISITS, STOCK)
    sentences = [{"claim_ids": ["b_value"], "text": "The lead value is 9000 rupees."}]
    out = run_gate(real_gate([{"sentences": sentences}, {"sentences": sentences}]), b)
    assert out.status in (ESCALATE, REWRITE) and out.status != SEND
    rec = Recorder()
    assert deliver(out, b, rec) is None and rec.intents == []


def test_nothing_is_delivered_when_the_gate_does_not_send():
    rec = Recorder()
    b = build_brief(rep(40), LEAD, VISITS, STOCK)
    for status in (ESCALATE, REWRITE, "NO_REPLY", "CLARIFY"):
        assert deliver(GateOutcome(status, "some text"), b, rec) is None
    assert deliver(GateOutcome(SEND, None), b, rec) is None and rec.intents == []


# ---------------------------------------------------------------- follow-ups
def test_follow_up_is_answered_from_the_same_sources_or_not_at_all():
    b = build_brief(rep(40), {**LEAD, "value_paise": 100_000}, VISITS, STOCK)             # short brief: the address was not in it
    (c,) = answer_followup(b, "What is the address?")
    assert c.text == "The address is 12 Main Road." and b.book.exists(c.source) and check_claims([c], b.book)[0].ok
    assert answer_followup(b, "Will it rain tomorrow?") is None
    thin = build_brief(rep(40), {k: v for k, v in LEAD.items() if k != "address"}, VISITS, STOCK)
    assert answer_followup(thin, "where is the shop") is None                              # asked, but the system has no address: no answer, not a guess
    out = run_gate(real_gate([{"sentences": [{"claim_ids": [c.id], "text": c.text}]}]), b, [c])
    assert out.status == SEND and out.text == "The address is 12 Main Road."
