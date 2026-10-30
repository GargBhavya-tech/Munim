"""Grounding steps 1-4 through GroundingVerifier: source check, numbers check, entailment, corroboration level. Logic tests use a fake NLI;
the last tests run the real entailment model over verbalised tool results."""
import os

import pytest

from core.gate.sources import Claim, SourceBook
from core.gate.verify import GroundingVerifier, verbalise
from tests.fakes_gate import FakeNLI

TOOL = "tool:get_settlements#call_1"
PLAN = "tool:get_plan#call_2"
KB = "kb:fee_schedule#std@v1"
POL = "policy:LOAN-RATE-001"


def book():
    b = SourceBook()
    b.add_tool_result(TOOL, {"settlement_id": "S-0100", "amount_paise": 820000, "status": "PENDING", "expected_at": "2026-10-31T10:00:00+05:30"})
    b.add_tool_result(PLAN, {"plan": "Soundbox Standard", "monthly_fee_paise": 14900})
    b.add_kb(KB, "Soundbox Standard costs 149 rupees a month. Soundbox Plus costs 249 rupees a month.")
    b.add_policy(POL, "Rates and approval are decided by the lender.")
    return b


def verify(claims, nli=None, **kw):
    return GroundingVerifier(nli or FakeNLI(), **kw).verify(claims, book())


def one(text, source=TOOL, nli=None, **kw):
    return verify([Claim("c1", text, source)], nli, **kw)[0]


# ---- verbalise --------------------------------------------------------------------------------------------------------
def test_tool_results_become_plain_sentences_with_rupees_and_readable_times():
    s = verbalise(TOOL, book().tool_results[TOOL])
    assert "Settlements amount is 8200 rupees." in s and "Settlements status is PENDING." in s and "Settlements settlement id is S-0100." in s
    assert "31 October 2026 at 10:00" in s and "2026-10-31T10:00:00+05:30" in s
    assert verbalise(PLAN, book().tool_results[PLAN]) == "Plan plan is Soundbox Standard. Plan monthly fee is 149 rupees."


def test_text_sources_pass_through_and_nested_or_list_results_are_flattened():
    assert verbalise(KB, "Soundbox Standard costs 149 rupees a month.") == "Soundbox Standard costs 149 rupees a month."
    s = verbalise("tool:get_device_status#1", {"device": {"status": "OFFLINE", "battery": 12}, "checks": [{"name": "reboot", "ok": False}], "online": True})
    assert "Device device status is OFFLINE." in s and "Device device battery is 12." in s      # nested keys keep their parent in the sentence
    assert "Device checks item 1 name is reboot." in s and "Device checks item 1 ok is no." in s and "Device online is yes." in s
    assert s.count("OFFLINE") == 1                                                          # each value appears once


# ---- steps 1 and 2: source and numbers --------------------------------------------------------------------------------
def test_a_claim_whose_source_does_not_exist_is_unsupported_with_a_reason():
    v = one("The amount is 8200 rupees.", source="tool:get_nothing#9")
    assert (v.level, v.sendable, v.color) == ("UNSUPPORTED", False, "red") and v.reason.startswith("source_not_found")


def test_an_invented_number_is_blocked_before_the_model_is_asked():
    nli = FakeNLI()
    v = one("The amount is 9900 rupees.", nli=nli)
    assert not v.sendable and v.reason == "numbers_not_in_source: 9900" and v.numbers_ok is False and nli.calls == []


def test_the_number_must_be_in_the_cited_source_not_in_another():
    v = one("The amount is 149 rupees.", source=TOOL)                  # 149 exists in the plan, not in the settlement
    assert not v.sendable and v.reason.startswith("numbers_not_in_source")


# ---- step 3: entailment -----------------------------------------------------------------------------------------------
def test_a_correct_claim_is_sendable_and_the_source_is_the_premise():
    nli = FakeNLI()
    v = one("The amount is 8200 rupees and the status is PENDING.", nli=nli)
    assert v.sendable and v.level == "SUPPORTED" and v.color == "green" and v.nli_label == "ENTAILMENT"
    premise, hypothesis = nli.calls[0]
    assert premise.startswith("Settlements") and hypothesis == "The amount is 8200 rupees and the status is PENDING."


def test_a_neutral_claim_is_blocked():
    v = one("The settlement will arrive tomorrow.")
    assert not v.sendable and v.level == "UNSUPPORTED" and "does not entail" in v.reason and v.nli_label == "NEUTRAL"


def test_a_contradicted_claim_is_blocked_as_contradicted():
    v = one("The settlement has been paid.", nli=FakeNLI(contradict=[("PENDING", "has been paid")]))
    assert not v.sendable and v.level == "CONTRADICTED" and "contradicts" in v.reason


# ---- step 4: corroboration --------------------------------------------------------------------------------------------
def test_a_tool_claim_that_an_approved_document_also_entails_is_corroborated():
    v = one("Soundbox Standard 149 rupees", source=PLAN)
    assert v.sendable and v.level == "CORROBORATED" and v.corroborated_by == (KB,)


def test_another_source_can_raise_a_claim_but_never_rescue_one():
    v = one("Soundbox Standard costs 149 rupees a month.", source=PLAN)      # the cited plan result does not say "costs"; the KB does
    assert not v.sendable and v.level == "UNSUPPORTED" and v.corroborated_by == ()


def test_only_the_cited_source_can_contradict_and_an_unrelated_source_never_blocks():
    claim = "Soundbox Standard 149 rupees"
    cited = one(claim, source=KB, nli=FakeNLI(contradict=[("149 rupees", "Soundbox")]))           # contradiction from the CITED kb itself
    assert cited.level == "CONTRADICTED" and not cited.sendable
    # the model misreading an unrelated tool or policy text as a contradiction (measured in #51) must not block a claim its own source entails
    for other in ("Plan plan is", "decided by the lender"):
        ok = one(claim, source=KB, nli=FakeNLI(contradict=[(other, "Soundbox")]))
        assert ok.sendable and ok.level in ("SUPPORTED", "CORROBORATED"), other


class QuirkNLI(FakeNLI):
    """Reproduces the measured quirk: a claim that is an EXACT sentence of a longer premise is labelled NEUTRAL."""

    def label_batch(self, pairs):
        neutral = ("NEUTRAL", {"ENTAILMENT": 0.0, "NEUTRAL": 1.0, "CONTRADICTION": 0.0})
        return [neutral if (len(p) > len(h) + 5 and h in p) else r for (p, h), r in zip(pairs, super().label_batch(pairs))]


def test_a_claim_that_is_an_exact_sentence_of_a_longer_document_is_still_entailed():
    nli = QuirkNLI()
    v = one("Soundbox Plus costs 249 rupees a month.", source=KB, nli=nli)                       # the whole kb text says NEUTRAL; its sentence says ENTAILMENT
    assert v.sendable and v.nli_label == "ENTAILMENT"
    assert any(p == "Soundbox Plus costs 249 rupees a month." for p, _ in nli.calls) and nli.calls[0][0].startswith("Soundbox Standard costs")   # whole first


def test_one_unrelated_sentence_cannot_make_a_claim_contradicted():
    only_a_sentence = FakeNLI(contradict=[("Soundbox Standard costs 149 rupees a month.", "Plus")])
    v = one("Soundbox Plus costs 249 rupees a month.", source=KB, nli=only_a_sentence)
    assert v.sendable                                                                           # entailed by its own sentence; a lone sentence cannot block
    whole = FakeNLI(contradict=[("Soundbox Standard costs 149 rupees a month. Soundbox Plus costs 249 rupees a month.", "Soundbox Plus costs 249")], neutral=[])
    assert one("Soundbox Plus costs 249 rupees a month.", source=KB, nli=whole).sendable        # an entailing sentence wins over a whole-source misfire


def test_a_forbidding_rule_blocks_the_claim_and_names_the_rule():
    v = one("The amount is 8200 rupees.", forbid_check=lambda c: "LOAN-RATE-001")
    assert not v.sendable and v.level == "CONTRADICTED" and v.rule_id == "LOAN-RATE-001" and "LOAN-RATE-001" in v.reason


def test_every_verdict_has_a_chip_with_reason_for_the_dashboard():
    vs = verify([Claim("c1", "The amount is 8200 rupees.", TOOL), Claim("c2", "The amount is 1 rupees.", TOOL)])
    chips = [v.as_chip() for v in vs]
    assert chips[0]["color"] == "green" and chips[0]["reason"] is None
    assert chips[1]["color"] == "red" and chips[1]["reason"] and set(chips[1]) == {"id", "text", "source", "level", "color", "reason", "rule_id"}


# ---- the real entailment model over verbalised tool results ------------------------------------------------------------
needs_models = pytest.mark.skipif(os.environ.get("MUNIM_SKIP_MODEL_TESTS") == "1", reason="model tests disabled")


@pytest.fixture(scope="module")
def real_nli():
    from core.gate.nli import EntailmentModel
    return EntailmentModel()


@needs_models
def test_real_model_accepts_true_statements_about_a_verbalised_tool_result(real_nli):
    b = SourceBook()
    b.add_tool_result("tool:get_device_status#1", {"device_id": "D-0001", "status": "OFFLINE", "battery": 80})
    vs = GroundingVerifier(real_nli).verify([Claim("a", "The device status is OFFLINE.", "tool:get_device_status#1"),
                                             Claim("b", "The device battery is 80.", "tool:get_device_status#1")], b)
    assert [v.sendable for v in vs] == [True, True], [(v.text, v.nli_label, v.reason) for v in vs]


@needs_models
def test_real_model_blocks_a_contradiction_and_an_invented_promise(real_nli):
    b = SourceBook()
    b.add_tool_result("tool:get_device_status#1", {"device_id": "D-0001", "status": "OFFLINE", "battery": 80})
    vs = GroundingVerifier(real_nli).verify([Claim("a", "The device status is ONLINE.", "tool:get_device_status#1"),
                                             Claim("b", "The device will be replaced tomorrow.", "tool:get_device_status#1")], b)
    assert [v.sendable for v in vs] == [False, False], [(v.text, v.nli_label) for v in vs]
