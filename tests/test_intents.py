import collections
from pathlib import Path

import pytest
import yaml

from core.engine.intents import (
    CONTROL_INTENTS, INTENT_TO_PLAYBOOK, UNSUPPORTED_PLAYBOOK, detect_intent, language_switch_target, normalise_text, top_intent,
)

ROOT = Path(__file__).resolve().parent.parent
LABELLED = yaml.safe_load((ROOT / "packs/paytm_merchant/labelled/triage_messages.yaml").read_text(encoding="utf-8"))["messages"]

REQUIRED = {"REPORT_DEVICE_PROBLEM", "SETTLEMENT_MISSING", "CUSTOMER_PAYMENT_FAILED", "LOAN_INTEREST", "WANTS_HUMAN", "COMPLAINT",
            "VERIFY_PAYMENT", "ASK_WHICH_DOCS", "STOP", "UNDO_LAST", "REFUND_REQUEST", "CLOSURE_REQUEST", "CHANGE_BANK", "SWITCH_LANGUAGE"}


def test_all_required_intents_are_known_and_mapped():
    assert REQUIRED <= set(INTENT_TO_PLAYBOOK)
    assert INTENT_TO_PLAYBOOK["REPORT_DEVICE_PROBLEM"] == "C2_SOUNDBOX_FAULT"
    assert INTENT_TO_PLAYBOOK["WANTS_HUMAN"] == UNSUPPORTED_PLAYBOOK
    assert {"STOP", "UNDO_LAST", "SWITCH_LANGUAGE"} == CONTROL_INTENTS


def test_kannada_switch_language_message_maps_to_language_switch():
    assert top_intent("ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ") == "SWITCH_LANGUAGE"
    assert detect_intent("ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ")[0][0] == "SWITCH_LANGUAGE"
    assert language_switch_target("ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ") == "kn"
    assert language_switch_target("ಹಿಂದಿಯಲ್ಲಿ ಮಾತಾಡಿ") == "hi"
    assert language_switch_target("hindi mein baat karo") == "hi"
    assert language_switch_target("Please speak in English") == "en"


def test_indic_digits_are_normalised_first():
    assert normalise_text("पेमेंट ५०० रुपये ०१२") == "पेमेंट 500 रुपये 012"
    assert normalise_text("ಹಣ ೨೫೦ ೦೯") == "ಹಣ 250 09"
    assert normalise_text("  Hello,   WORLD!! ") == "hello world"


def test_kannada_script_matches_not_only_english():
    """Watch out: English-only lists / Kannada never matching."""
    assert top_intent("ನನಗೆ ಲೋನ್ ಬೇಕು") == "LOAN_INTEREST"
    assert top_intent("ಸೆಟಲ್‌ಮೆಂಟ್ ಬಂದಿಲ್ಲ") == "SETTLEMENT_MISSING"     # with a zero-width non-joiner, as people type it
    assert top_intent("ಸೆಟಲ್ಮೆಂಟ್ ಬಂದಿಲ್ಲ") == "SETTLEMENT_MISSING"      # and without
    assert top_intent("ಮೆಸೇಜ್ ಮಾಡಬೇಡಿ") == "STOP"
    assert top_intent("ನನ್ನ ಖಾತೆ ಮುಚ್ಚಬೇಕು") == "CLOSURE_REQUEST"


def test_devanagari_matches_with_and_without_nukta():
    assert top_intent("ग्राहक का पेमेंट फ़ेल हो गया") == "CUSTOMER_PAYMENT_FAILED"
    assert top_intent("ग्राहक का पेमेंट फेल हो गया") == "CUSTOMER_PAYMENT_FAILED"
    assert top_intent("कौन से दस्तावेज़ चाहिए") == top_intent("कौन से दस्तावेज चाहिए") == "ASK_WHICH_DOCS"


def test_hinglish_spelling_variants():
    for t in ("settlement nahi aaya", "Settlement nahi aya", "SETTLEMENT NAHI MILA"):
        assert top_intent(t) == "SETTLEMENT_MISSING", t


def test_short_kannada_stem_does_not_fire_inside_another_word():
    assert top_intent("ಸಾಲ ಬೇಕು") == "LOAN_INTEREST"
    assert top_intent("ಸಾಲು ಇದೆ") is None             # ಸಾಲು = a row / queue, not a loan


def test_latin_phrases_need_whole_words():
    assert top_intent("the loanword is fine") is None
    assert top_intent("please stop") == "STOP"
    assert top_intent("bus stop is near") == "STOP"      # a known keyword limit; the model signal is what disambiguates


def test_no_match_returns_empty():
    assert detect_intent("hello how are you") == []
    assert top_intent("") is None


def test_multi_intent_message_returns_both_ranked():
    r = detect_intent("Mera Soundbox band hai aur loan ka bhi batao")
    assert [i for i, _ in r] == ["REPORT_DEVICE_PROBLEM", "LOAN_INTEREST"]
    assert r[0][1] == 0.88 and r[1][1] == 0.8


def test_longest_phrase_breaks_ties_between_intents():
    assert top_intent("ಸೇವೆ ನಿಲ್ಲಿಸಿ") == "CLOSURE_REQUEST"   # both STOP ('ನಿಲ್ಲಿಸಿ') and CLOSURE match; the longer phrase wins


def test_language_hint_never_changes_the_winner_for_a_clear_message():
    assert top_intent("मुझे लोन चाहिए", "hi") == top_intent("मुझे लोन चाहिए", "en") == "LOAN_INTEREST"


def test_labelled_set_shape():
    assert len(LABELLED) == 100
    assert collections.Counter(m["lang"] for m in LABELLED) == {"hi": 25, "kn": 25, "en": 25, "hinglish": 25}
    assert {m["intent"] for m in LABELLED} <= set(INTENT_TO_PLAYBOOK)
    assert len({m["text"] for m in LABELLED}) == 100


def test_top1_accuracy_per_language_is_reported(capsys):
    hit, tot, misses = collections.Counter(), collections.Counter(), []
    for m in LABELLED:
        tot[m["lang"]] += 1
        got = top_intent(m["text"], m["lang"])
        if got == m["intent"]:
            hit[m["lang"]] += 1
        else:
            misses.append((m["lang"], m["text"], got, m["intent"]))
    with capsys.disabled():
        print()
        for lang in ("hi", "kn", "en", "hinglish"):
            print(f"  triage rule top-1 accuracy {lang:9s}: {hit[lang]}/{tot[lang]} = {hit[lang] / tot[lang]:.0%}")
        print(f"  overall: {sum(hit.values())}/100")
        for m in misses:
            print("   miss:", m)
    # no pass threshold (the number is the disagreement signal); only a sanity floor that proves the lists work at all
    assert sum(hit.values()) >= 50
    for lang in tot:
        assert hit[lang] >= 10, f"{lang} lists look broken"
