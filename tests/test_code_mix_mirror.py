"""#137 code-mix mirroring. Hand-labelled messages with exact token counts. Hindi and Kannada lines are simple and common; NATIVE-SPEAKER REVIEW EXPECTED."""
import json
import re

import pytest

from core.gate.faithfulness import FaithfulnessChecker
from core.gate.mirror import analyse, make_style_fn, new_language_violations, style_hint
from core.gate.phrases import Phrases
from core.gate.pipeline import SEND, GroundingGate
from core.gate.render import Renderer
from core.gate.sources import Claim, SourceBook
from core.gate.verify import GroundingVerifier
from tests.fakes_gate import FakeNLI, FakeTranslator, llm

TOOL = "tool:get_settlements#call_1"
C1 = Claim("c1", "The amount is 8200 rupees.", TOOL)
C2 = Claim("c2", "The status is PENDING.", TOOL)


# (message, hi, kn, en, native_script, bucket, script)
CASES = [
    ("mera settlement kab aaya hai", 4, 0, 1, 0, "native_leaning", "roman"),          # hinglish: 4/5 = 800 permille
    ("when will my settlement arrive", 0, 0, 5, 0, "mostly_english", "roman"),        # english
    ("मेरा पैसा अभी नहीं आया", 0 + 5, 0, 0, 5, "mostly_native", "native"),            # hindi, Devanagari
    ("ನನ್ನ ಹಣ ಬಂದಿಲ್ಲ", 0, 3, 0, 3, "mostly_native", "native"),                      # kannada, Kannada script
    ("mera payment pending hai please", 2, 0, 3, 0, "balanced", "roman"),             # 2/5 = 400 permille
    ("मेरा payment pending नहीं है", 3, 0, 2, 3, "native_leaning", "native"),          # mixed script, 3/5 = 600
]


@pytest.mark.parametrize("msg,hi,kn,en,nat,bucket,script", CASES)
def test_token_counts_are_exact(msg, hi, kn, en, nat, bucket, script):
    p = analyse([msg])
    assert (p.hi, p.kn, p.en, p.native_script) == (hi, kn, en, nat)


@pytest.mark.parametrize("msg,hi,kn,en,nat,bucket,script", CASES)
def test_bucket_and_script_follow_the_measured_ratio(msg, hi, kn, en, nat, bucket, script):
    p = analyse([msg])
    assert p.bucket == bucket and p.script == script


def test_permille_is_integer_and_exact():
    assert analyse(["mera settlement kab aaya hai"]).native_share_permille == 800
    assert analyse(["mera payment pending hai please"]).native_share_permille == 400
    assert analyse(["when will my settlement arrive"]).native_share_permille == 0
    assert analyse([""]).bucket == "unknown" and analyse([]).total == 0


def test_only_the_last_n_messages_count():
    msgs = ["when will my settlement arrive"] * 4 + ["mera paisa nahi aaya"]
    assert analyse(msgs, n=1).bucket == "mostly_native"
    assert analyse(msgs, n=5).native_share_permille == (1000 * 4) // (4 + 20)


def test_formality_comes_from_markers():
    assert analyse(["aap kripya batao"]).formality == "formal"
    assert analyse(["tum yaar batao"]).formality == "informal"
    assert analyse(["batao"]).formality == "neutral"
    assert analyse(["ನೀವು ದಯವಿಟ್ಟು ಹೇಳಿ"]).formality == "formal"


def test_hinglish_input_gets_a_hinglish_roman_hint_with_no_digits():
    p = analyse(["mera settlement kab aaya hai", "aap bataiye"])
    h = style_hint(p, "hinglish")
    assert "almost all Hindi with only a few English words" in h and "formal register" in h and "roman letters" in h
    assert "Use only these languages: English, Hindi" in h and "Kannada" not in h
    assert not re.search(r"\d", h)


def test_hint_never_names_a_language_the_merchant_did_not_use():
    h = style_hint(analyse(["mera paisa nahi aaya"]), "hi-IN")
    assert "Use only these languages: Hindi;" in h
    h2 = style_hint(analyse(["mera paisa nahi aaya"]), "kn-IN")          # the reply language itself is allowed, nothing else
    assert "Use only these languages: Hindi, Kannada;" in h2


def test_pure_english_or_nothing_measured_gives_no_hint():
    assert style_hint(analyse(["mera paisa"]), "en-IN") is None
    assert style_hint(analyse([]), "hinglish") is None


def test_new_language_script_in_a_reply_is_flagged():
    p = analyse(["mera paisa nahi aaya"])
    assert new_language_violations("ನಿಮ್ಮ ಹಣ 8200", p, "hinglish") == ["kn"]
    assert new_language_violations("aapka paisa 8200", p, "hinglish") == []
    assert new_language_violations("ನಿಮ್ಮ ಹಣ", p, "kn-IN") == []


def test_style_fn_adapter_feeds_the_engine_hook():
    fn = make_style_fn(lambda mid: {"M1": ["mera paisa nahi aaya"]}[mid], lambda mid: "hinglish")
    assert "Hindi" in fn("M1")


# ---- Watch out: mirroring breaking faithfulness. Real gate, scripted model.
def gate(replies, table=None):
    client, prov = llm(replies)
    nli = FakeNLI()
    return GroundingGate(GroundingVerifier(nli), Renderer(client, Phrases()), FaithfulnessChecker(nli, FakeTranslator(table))), prov


def book():
    b = SourceBook()
    b.add_tool_result(TOOL, {"settlement_id": "S-0100", "amount_paise": 820000, "status": "PENDING"})
    return b


HINT = style_hint(analyse(["mera settlement kab aaya hai"]), "hinglish")
MIRRORED = {"sentences": [{"claim_ids": ["c1"], "text": "Aapka amount 8200 rupees hai."}, {"claim_ids": ["c2"], "text": "Status PENDING hai."}]}
TABLE = {"Aapka amount 8200 rupees hai.": "The amount is 8200 rupees.", "Status PENDING hai.": "The status is PENDING."}


def test_a_mirrored_hinglish_reply_passes_the_full_gate_and_the_hint_reaches_the_renderer():
    g, prov = gate([MIRRORED], TABLE)
    out = g.run(claims=[C1, C2], book=book(), language="hinglish", style_hint=HINT)
    assert out.status == SEND and out.text == "Aapka amount 8200 rupees hai. Status PENDING hai."
    assert json.loads(prov.seen[0][1]["content"])["style"] == HINT


def test_mirroring_cannot_add_a_number_or_a_claim():
    bad = {"sentences": [{"claim_ids": ["c1"], "text": "Aapka amount 8200 rupees hai, fee 99 rupees."}, {"claim_ids": ["c2"], "text": "Status PENDING hai."}]}
    table = dict(TABLE, **{"Aapka amount 8200 rupees hai, fee 99 rupees.": "The amount is 8200 rupees. The fee is 99 rupees."})
    g, _ = gate([bad] * 4, table)
    out = g.run(claims=[C1, C2], book=book(), language="hinglish", style_hint=HINT)
    assert out.status != SEND and "99" not in (out.text if out.status == SEND else "")
    extra = {"sentences": [{"claim_ids": ["c1"], "text": "Aapka amount 8200 rupees hai."}, {"text": "Jaldi aa jayega."}]}   # a new unsourced sentence
    g2, _ = gate([extra] * 4, TABLE)
    assert g2.run(claims=[C1], book=book(), language="hinglish", style_hint=HINT).status != SEND
