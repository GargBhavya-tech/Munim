"""#52 hard escalation triggers: one fixture per trigger, per language for the keyword ones."""
from types import SimpleNamespace

import pytest
import yaml

from core.actions.types import ActionOutcome, Violation
from core.engine import escalation as esc
from core.engine.escalation import (HardTriggers, KeywordMatcher, Trigger, find_phrase, load_money_limit_paise, normalise)

CASE = SimpleNamespace(case_id="CS-0001", playbook_id="C1_SETTLEMENT")
EVENT = SimpleNamespace(event_id="E1", type="MERCHANT_MESSAGE", payload={})


@pytest.fixture(scope="module")
def ht():
    return HardTriggers()


def codes(ht, facts=None, event=EVENT):
    return [t.code for t in ht.check(CASE, event, facts or {})]


def test_nothing_fires_on_a_calm_turn(ht):
    assert ht.check(CASE, EVENT, {"text": "mera settlement kab aayega", "amount_paise": 400000, "settlement_status": "SETTLED",
                                  "history": [("A", "s"), ("B", "s"), ("A", "s")], "grounding_failures": 1,
                                  "faithfulness_failures": 1, "invalid_output_count": 1}) == []


# ------------------------------------------------------------------ wants a human
def test_wants_human_by_intent_flag_or_event_payload(ht):
    assert codes(ht, {"intent": "WANTS_HUMAN"}) == ["WANTS_HUMAN"]
    assert codes(ht, {"wants_human": True}) == ["WANTS_HUMAN"]
    ev = SimpleNamespace(payload={"intent": "WANTS_HUMAN"})
    assert codes(ht, {}, ev) == ["WANTS_HUMAN"]
    assert codes(ht, {"intent": "SETTLEMENT_MISSING"}) == []


# ------------------------------------------------------------------ regulatory keywords, per language
REGULATORY = [
    ("en", "I will complain to the banking ombudsman", "ombudsman"),
    ("en", "I am filing an RBI complaint today", "rbi"),
    ("en", "I will call the police", "police"),
    ("en", "my lawyer will send a notice", "lawyer"),
    ("en", "I will go to consumer court", "consumer court"),
    ("hinglish", "main lokpal ko likhunga", "lokpal"),
    ("hinglish", "mera vakil baat karega", "vakil"),
    ("hinglish", "main vakeel se baat karunga", "vakeel"),
    ("hinglish", "thana jaa raha hoon", "thana"),
    ("hi", "मैं लोकपाल के पास जाऊंगा", "लोकपाल"),
    ("hi", "मैं पुलिस में शिकायत करूंगा", "पुलिस"),
    ("hi", "मैं वकील से बात करूंगा", "वकील"),
    ("hi", "मैं उपभोक्ता फोरम जाऊंगा", "उपभोक्ता फोरम"),
    ("hi", "आरबीआई में शिकायत करूंगा", "आरबीआई"),
    ("hi", "मैं कानूनी कार्रवाई करूंगा", "कानूनी कार्रवाई"),
    ("kn", "ನಾನು ಪೊಲೀಸರಿಗೆ ಹೇಳ್ತೀನಿ", "ಪೊಲೀಸ"),
    ("kn", "ನನ್ನ ವಕೀಲರು ಮಾತಾಡ್ತಾರೆ", "ವಕೀಲ"),
    ("kn", "ನ್ಯಾಯಾಲಯಕ್ಕೆ ಹೋಗ್ತೀನಿ", "ನ್ಯಾಯಾಲಯ"),
    ("kn", "ಆರ್‌ಬಿಐ ಗೆ ದೂರು ಕೊಡ್ತೀನಿ", "ಆರ್ಬಿಐ"),            # typed with a zero-width non-joiner: still matches
    ("kn", "ಲೋಕಪಾಲ ಗೆ ಬರೀತೀನಿ", "ಲೋಕಪಾಲ"),
]


@pytest.mark.parametrize("lang,text,phrase", REGULATORY)
def test_regulatory_mention_fires_per_language(ht, lang, text, phrase):
    ts = ht.check(CASE, EVENT, {"text": text})
    assert [t.code for t in ts] == ["REGULATORY_MENTION"]
    assert phrase in ts[0].detail


def test_every_keyword_language_has_a_fixture_and_a_list():
    langs = KeywordMatcher().languages
    assert langs == ["en", "hi", "hinglish", "kn"]
    assert {l for l, _, _ in REGULATORY} == set(langs)


def test_every_listed_phrase_matches_itself_in_a_sentence():
    km = KeywordMatcher()
    for lang, phrases in km.regulatory.items():
        for p in phrases:
            assert km.hits(f"yes {p} is what I said") != [], (lang, p)


def test_devanagari_and_kannada_are_not_english_only():
    """Watch out of the ticket: an English-only list would miss every one of these."""
    en_only = KeywordMatcher(data={"regulatory": {"en": ["ombudsman", "police", "lawyer"]}})
    for text in ("मैं लोकपाल के पास जाऊंगा", "मैं पुलिस में शिकायत करूंगा", "ನಾನು ಪೊಲೀಸರಿಗೆ ಹೇಳ್ತೀನಿ"):
        assert en_only.hits(text) == []
        assert KeywordMatcher().hits(text) != []


# ------------------------------------------------------------------ word boundary and negation (documented decisions)
@pytest.mark.parametrize("text", ["the policy is unclear", "our policeman friend", "I am impolite", "this is a lawyerly tone", "caught in a courtyard",
                                  "he pursued me", "issue"])
def test_keyword_inside_an_unrelated_word_does_not_fire(ht, text):
    assert ht.check(CASE, EVENT, {"text": text}) == []


def test_plural_and_case_do_not_hide_a_keyword(ht):
    assert codes(ht, {"text": "ALL the Lawyers and POLICE"}) == ["REGULATORY_MENTION"]


def test_negated_mention_still_escalates_and_is_marked(ht):
    """DECISION: a human must see any legal or regulatory word, even 'I am not going to the police'. The detail records the negation."""
    ts = ht.check(CASE, EVENT, {"text": "I am not going to the police"})
    assert [t.code for t in ts] == ["REGULATORY_MENTION"]
    assert "(negated)" in ts[0].detail
    plain = ht.check(CASE, EVENT, {"text": "I am going to the police"})
    assert "(negated)" not in plain[0].detail


def test_negation_in_another_clause_is_not_attributed(ht):
    ts = ht.check(CASE, EVENT, {"text": "no problem. I will call the police"})
    assert "(negated)" not in ts[0].detail


def test_find_phrase_boundaries_directly():
    assert find_phrase("go to police now", "police") == 6
    assert find_phrase("policemen", "police") == -1
    assert find_phrase("पुलिसवाले आए", "पुलिस") == 0         # Indic: suffix allowed
    assert find_phrase("xपुलिस", "पुलिस") == -1              # but a letter on the LEFT is not a boundary
    assert normalise("ಆರ್‌ಬಿಐ") == normalise("ಆರ್ಬಿಐ")


# ------------------------------------------------------------------ the other triggers
def test_fraud_signal(ht):
    ts = ht.check(CASE, EVENT, {"fraud_flags": ["CHARGEBACK_RING"]})
    assert [(t.code, t.detail) for t in ts] == [("FRAUD_SIGNAL", "CHARGEBACK_RING")]


def test_name_mismatch_by_flag_and_by_bank_reason(ht):
    assert codes(ht, {"name_mismatch": True}) == ["NAME_MISMATCH"]
    assert codes(ht, {"bank.reason": "NAME_MISMATCH"}) == ["NAME_MISMATCH"]
    assert codes(ht, {"bank": {"reason": "NAME_MISMATCH"}}) == ["NAME_MISMATCH"]
    assert codes(ht, {"bank.reason": "ACCOUNT_CLOSED"}) == []


def test_money_limit_is_read_from_limits_yaml_not_hard_coded(ht, tmp_path):
    assert load_money_limit_paise() == 1_000_000 == ht.money_limit_paise
    assert codes(ht, {"amount_paise": 999_999}) == []
    assert codes(ht, {"amount_paise": 1_000_000}) == ["AMOUNT_ABOVE_LIMIT"]       # >= : the boundary itself escalates
    p = tmp_path / "limits.yaml"
    p.write_text(yaml.safe_dump({"money_limit": {"threshold_paise": 500_000}}))
    assert HardTriggers(limits_path=p).money_limit_paise == 500_000
    assert [t.code for t in HardTriggers(limits_path=p).check(CASE, EVENT, {"amount_paise": 500_000})] == ["AMOUNT_ABOVE_LIMIT"]


def test_settlement_held_risk(ht):
    assert codes(ht, {"settlement_status": "HELD_RISK"}) == ["SETTLEMENT_HELD_RISK"]
    assert codes(ht, {"settlement.status": "HELD_RISK"}) == ["SETTLEMENT_HELD_RISK"]
    assert codes(ht, {"settlement_status": "SETTLED"}) == []


def test_out_of_band_is_a_policy_escalate_only(ht):
    esc_v = Violation("WAIVER-BAND-001", "Waiver band", "ESCALATE", "6 months asked", route="sales_lead")
    warn_v = Violation("X-1", "warn", "WARN", "just a warning")
    block_v = Violation("SECRET-001", "block", "BLOCK", "blocked")
    ts = ht.check(CASE, EVENT, {"violations": [esc_v, warn_v, block_v]})
    assert [(t.code, t.detail) for t in ts] == [("OUT_OF_BAND", "WAIVER-BAND-001")]


def test_grounding_and_faithfulness_failed_twice(ht):
    assert codes(ht, {"grounding_failures": 1}) == []
    assert codes(ht, {"grounding_failures": 2}) == ["GROUNDING_FAILED_TWICE"]
    assert codes(ht, {"faithfulness_failures": 1}) == []
    assert codes(ht, {"faithfulness_failures": 2}) == ["FAITHFULNESS_FAILED_TWICE"]
    assert codes(ht, {"grounding_failures": 2, "faithfulness_failures": 2}) == ["GROUNDING_FAILED_TWICE", "FAITHFULNESS_FAILED_TWICE"]


def test_invalid_model_output_twice(ht):
    assert codes(ht, {"invalid_output_count": 1}) == []
    assert codes(ht, {"invalid_output_count": 2}) == ["INVALID_OUTPUT_TWICE"]


def test_tool_failure_after_retries_uses_the_executor_error_prefixes(ht):
    circuit = ActionOutcome("ERROR", "k", error="CIRCUIT_OPEN: bank is unavailable (circuit open, next probe in 30s); nothing was sent")
    upstream = ActionOutcome("ERROR", "k", error="UPSTREAM_UNAVAILABLE: kyc did not answer (timeout) after 4 attempt(s)")
    other = ActionOutcome("ERROR", "k", error="INVALID_ARGS: merchant_id: field required")
    ok = ActionOutcome("OK", "k")
    assert codes(ht, {"outcomes": [circuit]}) == ["TOOL_FAILURE"]
    assert codes(ht, {"outcomes": [upstream]}) == ["TOOL_FAILURE"]
    assert codes(ht, {"outcomes": [ok, other]}) == []          # a business error is not an outage
    assert codes(ht, {"outcomes": ["UPSTREAM_UNAVAILABLE: x"]}) == ["TOOL_FAILURE"]
    assert [t.detail for t in ht.check(CASE, EVENT, {"outcomes": [upstream, circuit]})] == ["UPSTREAM_UNAVAILABLE"]   # reported once


def test_loop_needs_same_intent_same_step_three_turns_in_a_row(ht):
    same = ("SETTLEMENT_MISSING", "settlement")
    assert codes(ht, {"history": [same, same]}) == []
    assert codes(ht, {"history": [same, same, same]}) == ["LOOP"]
    assert codes(ht, {"history": [("A", "s")] + [same] * 3}) == ["LOOP"]
    assert codes(ht, {"history": [same, same, ("SETTLEMENT_MISSING", "bank")]}) == []        # step changed
    assert codes(ht, {"history": [same, same, ("OTHER", "settlement")]}) == []               # intent changed
    assert codes(ht, {"history": [same, ("OTHER", "settlement"), same]}) == []               # not consecutive
    assert codes(ht, {"history": [[None, "s"]] * 3}) == []                                    # no intent at all is not a loop


def test_playbook_escalate_when_is_reused_from_diagnose():
    ts = HardTriggers().check(CASE, EVENT, {"playbook_reasons": ["bank.reason == 'NAME_MISMATCH'"]})
    assert ts == [Trigger("PLAYBOOK_ESCALATE_WHEN", "bank.reason == 'NAME_MISMATCH'")]


def test_playbook_reasons_through_the_real_diagnose(db_path, clock):
    """End to end with the real S1 playbook: diagnose.escalation_reasons feeds the trigger."""
    from tests.world import World
    from core.engine.diagnose import escalation_reasons
    w = World(db_path, clock)
    w.control("/mock/control/bank/{mid}", status="FAILED", reason="NAME_MISMATCH")
    pb = w.playbooks["S1_ACTIVATION"]
    reasons = escalation_reasons(pb, w.reader, w.mid, {"touches": 0})
    assert reasons == ["bank.reason == 'NAME_MISMATCH'"]
    hook = HardTriggers().as_engine_hook(lambda case, ev: {"playbook_reasons": escalation_reasons(pb, w.reader, w.mid, {"touches": 0})})
    assert hook(CASE, EVENT) == ["PLAYBOOK_ESCALATE_WHEN: bank.reason == 'NAME_MISMATCH'"]


# ------------------------------------------------------------------ engine adapter
def test_engine_hook_returns_strings_and_empty_when_calm():
    hook = HardTriggers().as_engine_hook(lambda case, ev: {"settlement_status": "HELD_RISK", "amount_paise": 1_240_000})
    assert hook(CASE, EVENT) == ["AMOUNT_ABOVE_LIMIT", "SETTLEMENT_HELD_RISK"]
    assert HardTriggers().as_engine_hook()(CASE, EVENT) == []


def test_text_comes_from_the_event_payload_when_not_in_facts():
    ev = SimpleNamespace(payload={"text": "main police ko bataunga"})
    assert [t.code for t in HardTriggers().check(CASE, ev, {})] == ["REGULATORY_MENTION"]


def test_any_one_trigger_is_enough_and_all_are_reported(ht):
    ts = codes(ht, {"wants_human": True, "text": "lawyer", "fraud_flags": ["X"], "name_mismatch": True, "amount_paise": 2_000_000,
                    "settlement_status": "HELD_RISK", "grounding_failures": 2, "faithfulness_failures": 2, "invalid_output_count": 2})
    assert ts == ["WANTS_HUMAN", "REGULATORY_MENTION", "FRAUD_SIGNAL", "NAME_MISMATCH", "AMOUNT_ABOVE_LIMIT", "SETTLEMENT_HELD_RISK",
                  "GROUNDING_FAILED_TWICE", "FAITHFULNESS_FAILED_TWICE", "INVALID_OUTPUT_TWICE"]


def test_trigger_constants_cover_the_ticket_list():
    required = {"WANTS_HUMAN", "REGULATORY_MENTION", "FRAUD_SIGNAL", "NAME_MISMATCH", "AMOUNT_ABOVE_LIMIT", "SETTLEMENT_HELD_RISK", "OUT_OF_BAND",
                "GROUNDING_FAILED_TWICE", "FAITHFULNESS_FAILED_TWICE", "TOOL_FAILURE", "LOOP", "INVALID_OUTPUT_TWICE", "PLAYBOOK_ESCALATE_WHEN"}
    assert required <= {v for k, v in vars(esc).items() if k.isupper() and isinstance(v, str) and v == k}
