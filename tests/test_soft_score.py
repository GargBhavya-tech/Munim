"""#53 soft score: the Bible's two worked examples, signal derivations, tiers, and "never the model's stated confidence"."""
import dataclasses
import json

import pytest

from core.engine.escalation import (BLUE, DEFAULT_CONFIG, RED, WHITE, ScoreConfig, SoftScorer, frustration, novelty, rule_sentiment,
                                    soft_score, speech_uncertainty, tier_for, value_at_risk)
from core.engine.triage import triage_message


def test_worked_example_one_is_exactly_0_375_and_does_not_escalate():
    s, parts = soft_score(frustration=0.9, intent_uncertainty=0.2, novelty=0.0, speech_uncertainty=0.1, value_at_risk=0.4)
    assert s == 0.375
    assert parts == {"frustration": 0.27, "intent_uncertainty": 0.05, "novelty": 0.0, "speech_uncertainty": 0.015, "value_at_risk": 0.04}
    assert tier_for(s)[0] == WHITE


def test_worked_example_two_is_exactly_0_65_and_escalates():
    s, parts = soft_score(frustration=0.9, intent_uncertainty=0.2, novelty=1.0, speech_uncertainty=0.6, value_at_risk=0.4)
    assert s == 0.65
    assert parts["novelty"] == 0.2 and parts["speech_uncertainty"] == 0.09
    assert tier_for(s) == (RED, "SOFT_SCORE")


def test_worked_examples_from_raw_observations():
    """Same two examples, but built from observations: 3 angry words + repeat, Rs 4,000, clean / noisy audio."""
    sc = SoftScorer()
    common = dict(sentiment=0.7, repeated_complaint=True, intent_uncertainty=0.2, amount_paise=400_000)
    one = sc.assess(retrieval_score=0.9, stt_confidence=0.9, **common)
    assert one.components["frustration"] == pytest.approx(0.9) and one.score == 0.375 and one.tier == WHITE and not one.escalate
    two = sc.assess(retrieval_score=0.1, stt_confidence=0.4, **common)
    assert two.score == 0.65 and two.tier == RED and two.escalate and two.reason == "SOFT_SCORE"


def test_weights_sum_to_one_and_defaults_are_the_bibles():
    c = ScoreConfig()
    assert (c.w_frustration, c.w_intent_uncertainty, c.w_novelty, c.w_speech_uncertainty, c.w_value_at_risk) == (0.30, 0.25, 0.20, 0.15, 0.10)
    assert (c.escalate_at, c.blue_at) == (0.60, 0.45)
    assert sum([c.w_frustration, c.w_intent_uncertainty, c.w_novelty, c.w_speech_uncertainty, c.w_value_at_risk]) == pytest.approx(1.0)
    assert soft_score(frustration=1, intent_uncertainty=1, novelty=1, speech_uncertainty=1, value_at_risk=1)[0] == 1.0


# ------------------------------------------------------------------ tiers
@pytest.mark.parametrize("s,tier", [(0.0, WHITE), (0.449999, WHITE), (0.45, BLUE), (0.5, BLUE), (0.599999, BLUE), (0.60, RED), (0.9, RED)])
def test_tier_thresholds(s, tier):
    assert tier_for(s)[0] == tier


def test_a_second_ambiguous_turn_escalates():
    assert tier_for(0.5, prior_blue_turns=0) == (BLUE, None)
    assert tier_for(0.5, prior_blue_turns=1) == (RED, "BLUE_REPEATED")
    assert tier_for(0.2, prior_blue_turns=3)[0] == WHITE        # a clear turn after BLUE is fine


def test_red_for_an_unverifiable_claim_or_outside_knowledge_even_with_a_low_score():
    assert tier_for(0.1, claim_unverifiable=True) == (RED, "UNVERIFIABLE_CLAIM")
    assert tier_for(0.1, outside_knowledge=True) == (RED, "OUTSIDE_KNOWLEDGE")


def test_blue_means_ask_one_question_not_escalate():
    r = SoftScorer().assess(intent_uncertainty=1.0, retrieval_score=0.1, stt_confidence=0.5)   # 0.25 + 0.2 + 0.075 = 0.525
    assert r.score == 0.525 and r.tier == BLUE and r.ask_clarifying_question and not r.escalate


# ------------------------------------------------------------------ never the model's stated confidence
def test_model_stated_confidence_is_ignored():
    sc = SoftScorer()
    obs = dict(text="ye bakwas hai, kitni baar", repeated_complaint=True, intent_uncertainty=0.8, retrieval_score=0.1, amount_paise=900_000)
    high = json.loads('{"intent": "SETTLEMENT_MISSING", "confidence": 0.99, "answer": "ok"}')
    low = json.loads('{"intent": "SETTLEMENT_MISSING", "confidence": 0.01, "answer": "ok"}')
    a, b, c = sc.assess(model_output=high, **obs), sc.assess(model_output=low, **obs), sc.assess(**obs)
    assert a == b == c
    assert a.escalate                      # and a 0.99-confident model does not stop the escalation


def test_overconfident_model_cannot_suppress_a_high_score_and_underconfident_cannot_raise_a_low_one():
    sc = SoftScorer()
    assert not sc.assess(model_output={"confidence": 0.01}, retrieval_score=0.9, intent_uncertainty=0.1).escalate
    assert sc.assess(model_output={"confidence": 1.0}, sentiment=1.0, intent_uncertainty=1.0, retrieval_score=0.0).escalate


# ------------------------------------------------------------------ signal derivations
def test_frustration_adds_0_2_for_a_repeat_and_caps_at_one():
    assert frustration(0.5, False) == 0.5
    assert frustration(0.5, True) == pytest.approx(0.7)
    assert frustration(0.9, True) == 1.0
    assert frustration(0.0, True) == pytest.approx(0.2)


@pytest.mark.parametrize("text,expected", [
    ("mera settlement kab aayega", 0.0),
    ("this is useless", 0.4),
    ("useless and a waste of time", 0.6),
    ("ye bakwas hai", 0.4),
    ("बहुत बेकार सर्विस है", 0.4),
    ("बकवास और धोखा", 0.6),
    ("ನನಗೆ ತುಂಬಾ ಕೋಪ ಬಂದಿದೆ", 0.4),
    ("bakwas bekar faltu dhokha pareshan gussa", 1.0),
])
def test_rule_sentiment_per_language(text, expected):
    assert rule_sentiment(text) == pytest.approx(expected)


def test_sentiment_model_hook_can_add_but_not_hide_anger():
    calm_text = "mera settlement kab aayega"
    assert SoftScorer(sentiment_fn=lambda t, l: 0.8).assess(text=calm_text).components["frustration"] == 0.8
    assert SoftScorer(sentiment_fn=lambda t, l: 0.0).assess(text="ye bakwas hai").components["frustration"] == 0.4


def test_novelty_scale():
    assert novelty(0.0) == 1.0 and novelty(0.34) == 1.0            # below the floor: outside approved knowledge
    assert novelty(0.35) == 1.0                                       # the floor itself is the top of the linear part
    assert novelty(0.55) == pytest.approx(0.5)
    assert novelty(0.75) == 0.0 and novelty(1.0) == 0.0
    assert novelty(None) == 0.0                                       # no knowledge lookup this turn
    with pytest.raises(ValueError):
        novelty(float("nan"))


def test_speech_uncertainty_derivation():
    assert speech_uncertainty(stt_confidence=0.9) == pytest.approx(0.1)
    assert speech_uncertainty(stt_confidence=0.9, language_flip=True) == 1.0         # a flip beats a good confidence
    assert speech_uncertainty(language_probability=0.8) == pytest.approx(0.2)       # Sarvam REST: only language_probability exists
    assert speech_uncertainty(stt_confidence=0.9, language_probability=0.5) == pytest.approx(0.1)   # a real confidence wins over the proxy
    assert speech_uncertainty() == 0.0                                                # typed message: no speech signal


def test_value_at_risk_is_amount_over_10000_rupees_capped():
    assert value_at_risk(400_000) == 0.4
    assert value_at_risk(1_000_000) == 1.0 and value_at_risk(5_000_000) == 1.0
    assert value_at_risk(0) == 0.0 and value_at_risk(None) == 0.0


def test_intent_uncertainty_arrives_from_triage():
    t = triage_message("paisa nahi aaya", [("SETTLEMENT_MISSING", 0.48), ("CUSTOMER_PAYMENT_FAILED", 0.42)], None,
                       {"SETTLEMENT_MISSING": "C1_SETTLEMENT", "CUSTOMER_PAYMENT_FAILED": "C3_PAYMENT_FAILED"})
    assert t.uncertainty == pytest.approx(0.94)
    r = SoftScorer().assess(intent_uncertainty=t.uncertainty, retrieval_score=0.9)
    assert r.components["intent_uncertainty"] == pytest.approx(0.94) and r.score == pytest.approx(0.235)


# ------------------------------------------------------------------ config: frozen, hashable, validated
def test_config_is_frozen_hashable_and_hashes_stably():
    c = ScoreConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.escalate_at = 0.1
    assert hash(c) == hash(ScoreConfig.from_dict({}))
    assert c.config_hash() == ScoreConfig.from_dict(dict(DEFAULT_CONFIG)).config_hash()
    assert c.config_hash() != ScoreConfig.from_dict({"escalate_at": 0.7}).config_hash()
    assert {c: 1}[ScoreConfig()] == 1


def test_changed_config_changes_the_decision():
    s = dict(frustration=0.9, intent_uncertainty=0.2, novelty=1.0, speech_uncertainty=0.6, value_at_risk=0.4)
    strict = ScoreConfig.from_dict({"escalate_at": 0.7})
    assert tier_for(0.65, strict)[0] == BLUE
    assert SoftScorer(strict).assess(sentiment=0.7, repeated_complaint=True, intent_uncertainty=0.2, retrieval_score=0.1,
                                     stt_confidence=0.4, amount_paise=400_000).tier == BLUE
    assert soft_score(config=ScoreConfig.from_dict({"w_novelty": 0.0}), **s)[0] == 0.45


def test_config_rejects_unknown_keys_and_bad_thresholds():
    with pytest.raises(ValueError):
        ScoreConfig.from_dict({"w_confidence": 0.5})            # no weight for model confidence can be configured in
    with pytest.raises(ValueError):
        ScoreConfig.from_dict({"blue_at": 0.7, "escalate_at": 0.6})
    assert "confidence" not in " ".join(DEFAULT_CONFIG)
