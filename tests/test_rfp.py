"""#120 E6: every RFP answer passes the grounding gate; an uncited or invented claim never reaches the response."""
from core.gate.sources import Claim, check_claim, SourceBook
from jobs import rfp
from tests.fakes_gate import FakeNLI

KB = "kb:refund_policy#limits@v1"
P_REFUND = rfp.Passage(KB, "refund_policy", "REVIEWED", "A refund cannot be more than the original payment. Large amounts are checked by a person.")
P_FEE = rfp.Passage("kb:fee_schedule#plan@v1", "fee_schedule", "REVIEWED", "The Soundbox rental is 149 rupees per month.")
RFP = "Intro text ignored.\nQ1. How are refund limits handled for large amounts?\nQ2. Describe your quantum teleportation roadmap.\n"


def run(writer=None, facts=None, passages=(P_REFUND, P_FEE)):
    return rfp.draft_rfp_response(RFP, FakeNLI(), passages=list(passages), system_facts=facts, claim_writer=writer)


def test_questions_are_parsed():
    assert rfp.parse_questions(RFP) == [("Q1", "How are refund limits handled for large amounts?"), ("Q2", "Describe your quantum teleportation roadmap.")]


def test_answer_is_built_only_from_verified_kb_sentences_with_citations():
    r = run()
    a1, a2 = r.answers
    assert a1.status == "ANSWERED"
    assert a1.text == "A refund cannot be more than the original payment. Large amounts are checked by a person."
    assert a1.citations == [KB] and all(c.source == KB for c in a1.claims)
    assert a2.status == "NEEDS_HUMAN" and a2.text == "" and a2.citations == []      # nothing to cite: no answer, not a guess
    assert r.label == "SIMULATED" and "[SIMULATED]" in r.render() and "NEEDS_HUMAN" in r.render()


def test_invented_number_is_dropped():
    def writer(q, cands):
        return [Claim("c1", "A refund cannot be more than the original payment.", KB), Claim("c2", "Refunds above 5000 rupees are free.", KB)]
    a = run(writer).answers[0]
    assert [c.id for c in a.claims] == ["c1"]
    assert a.dropped == [{"claim_id": "c2", "source": KB, "reason": "numbers_not_in_source: 5000"}]
    assert "5000" not in a.text


def test_claim_citing_a_source_not_in_the_turn_is_dropped():
    def writer(q, cands):
        return [Claim("c1", "A refund cannot be more than the original payment.", "kb:made_up#x@v1")]
    a = run(writer).answers[0]
    assert a.status == "NEEDS_HUMAN" and a.dropped[0]["reason"] == "source_not_found: kb:made_up#x@v1"


def test_unentailed_claim_is_dropped_even_with_a_real_source():
    def writer(q, cands):
        return [Claim("c1", "Refunds arrive within a day.", KB)]
    a = run(writer).answers[0]
    assert a.status == "NEEDS_HUMAN" and "does not entail" in a.dropped[0]["reason"]


def test_system_fact_numbers_come_from_the_read():
    facts = {"tool:get_stats#call_1": {"merchants_served": 1200}}
    def writer(q, cands):
        return [Claim("c1", "Merchants served is 1200.", "tool:get_stats#call_1"), Claim("c2", "Merchants served is 1300.", "tool:get_stats#call_1")]
    a = run(writer, facts).answers[0]
    assert [c.id for c in a.claims] == ["c1"] and a.dropped[0]["claim_id"] == "c2"


def test_every_claim_in_every_answer_passes_check_claim():
    r = rfp.draft_rfp_response(RFP, FakeNLI(), passages=[P_REFUND, P_FEE])
    book = SourceBook()
    book.add_kb(KB, P_REFUND.text)
    for a in r.answers:
        for c in a.claims:
            assert check_claim(c, book).ok


def test_real_kb_directory_loads_and_answers_a_refund_question():
    r = rfp.draft_rfp_response("Q1. Can a customer refund be cancelled after the merchant confirms it?", FakeNLI())
    a = r.answers[0]
    assert a.status == "ANSWERED" and all(c.startswith("kb:refund_policy#") for c in a.citations)


def test_intent_feed():
    feed = rfp.detect_intents([
        {"id": "B1", "buyer": "Synthetic Retail Ltd", "text": "We have 40 outlets and want a pilot demo; what is your pricing?"},
        {"id": "B2", "buyer": "Synthetic Bank", "text": "Security audit and API integration needed for our tender."},
        {"id": "B3", "buyer": "Nobody", "text": "Hello there."}])
    got = {(r["input_id"], r["intent"]) for r in feed["feed"]}
    assert got == {("B1", "VOLUME_DEPLOYMENT"), ("B1", "DEMO_REQUEST"), ("B1", "PRICING_ENQUIRY"), ("B2", "SECURITY_REVIEW"),
                   ("B2", "INTEGRATION_ENQUIRY"), ("B2", "TENDER_RFP")}
    assert feed["label"] == "SIMULATED"
