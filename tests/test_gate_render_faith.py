"""#47 renderer, #48 faithfulness check, #49 compliance phrases. Fakes for the model, NLI and translator; the real code for everything else."""
import json

import httpx
import numpy as np
import pytest

from core.gate.compliance import ComplianceChecker, is_ai_question, language_ok
from core.gate.faithfulness import FaithfulnessChecker, SarvamTranslator
from core.gate.phrases import Phrases, canon
from core.gate.render import RenderedReply, RenderedSentence, RenderError, Renderer
from core.gate.verify import ClaimVerdict
from core.llm.sarvam_http import SarvamHTTP, load_config
from tests.fakes_gate import FakeNLI, FakeTranslator, llm

PH = Phrases()


def V(cid, text, sendable=True):
    return ClaimVerdict(cid, text, "tool:x#1", "SUPPORTED" if sendable else "UNSUPPORTED", sendable, "green" if sendable else "red", None if sendable else "no")


C1 = V("c1", "The settlement amount is 8200 rupees.")
C2 = V("c2", "The settlement status is PENDING.")
BLOCKED = V("c3", "The settlement will arrive tomorrow.", sendable=False)


def claim_s(text, *ids):
    return {"claim_ids": list(ids), "text": text}


def render(replies, verdicts=(C1, C2, BLOCKED), language="en-IN", **kw):
    client, prov = llm(replies)
    r = Renderer(client, PH).render(list(verdicts), language, **kw)
    return r, prov


# ================================================================== #47 renderer
def test_a_reply_is_claim_sentences_plus_fixed_phrases_expanded_by_code():
    r, prov = render([{"sentences": [{"phrase": "greeting"}, claim_s("Your settlement is 8200 rupees.", "c1"), claim_s("It is pending.", "c2"), {"phrase": "thank_you"}]}],
                     language="hinglish")
    assert [s.kind for s in r.sentences] == ["phrase", "claim", "claim", "phrase"]
    assert r.sentences[0].text == "Namaste." and r.sentences[3].text == "Dhanyavaad."          # the approved text, whatever the model typed
    assert r.sentences[1].claim_ids == ("c1",) and r.text.startswith("Namaste. Your settlement")


def test_the_request_carries_only_verified_claims_and_only_conversational_phrases():
    _, prov = render([{"sentences": [claim_s("x 8200", "c1")]}])
    req = json.loads(prov.seen[0][1]["content"])
    assert [c["id"] for c in req["claims"]] == ["c1", "c2"]                                   # the blocked claim never reaches the renderer
    assert set(req["allowed_phrases"]) == {"greeting", "one_moment", "thank_you", "anything_else"} and "identity" not in req["allowed_phrases"]
    assert "Rules:" in prov.seen[0][0]["content"] and "Add nothing" in prov.seen[0][0]["content"]


@pytest.mark.parametrize("bad", [
    {"sentences": [{"text": "It will be fixed soon."}]},                                                  # reassurance with no claim ids
    {"sentences": [claim_s("It will be fixed soon.", "c9")]},                                             # cites a claim that does not exist
    {"sentences": [claim_s("It will arrive tomorrow.", "c3")]},                                           # cites a BLOCKED claim
    {"sentences": [{"phrase": "identity"}]},                                                              # a compliance phrase is not the renderer's to use
    {"sentences": [{"phrase": "made_up"}]},
    {"sentences": [{"phrase": "thank_you", "text": "Thanks a ton, have a great day!"}]},                  # rewording a fixed phrase
    {"sentences": [{"phrase": "greeting"}] * 4},                                                          # too many phrases
    {"sentences": [claim_s("a", "c1"), claim_s("b", "c1"), claim_s("c", "c2")]},                          # more claim sentences than claims
    {"sentences": []}, {"sentences": [claim_s("", "c1")]}, {"sentences": [claim_s("x", "c1", )], "extra": 1},
    {"reply": "hello"}, "not json at all",
])
def test_anything_beyond_the_claims_is_rejected(bad):
    with pytest.raises(RenderError):
        render([bad, bad])


def test_one_invalid_reply_is_retried_with_the_error_and_a_second_is_final():
    r, prov = render([{"sentences": [{"text": "soon"}]}, {"sentences": [claim_s("Amount 8200.", "c1")]}])
    assert len(prov.seen) == 2 and prov.seen[1][2]["content"].startswith("INVALID_RENDER:") and r.sentences[0].claim_ids == ("c1",)
    with pytest.raises(RenderError) as e:
        render([{"sentences": []}, {"sentences": []}])
    assert "invalid twice" in str(e.value)


def test_feedback_and_style_reach_the_second_render():
    _, prov = render([{"sentences": [claim_s("Amount 8200.", "c1")]}], feedback="sentence 1: not entailed", style_hint="mirror Hinglish")
    req = json.loads(prov.seen[0][1]["content"])
    assert req["feedback"] == "sentence 1: not entailed" and req["style"] == "mirror Hinglish"


def test_phrase_only_reply_is_allowed_when_there_are_no_claims():
    r, _ = render([{"sentences": [{"phrase": "one_moment"}]}], verdicts=[])
    assert r.sentences[0].text == "One moment, please."


# ================================================================== #48 faithfulness
def reply(language, *sentences):
    return RenderedReply(language, list(sentences))


def cs(text, *ids):
    return RenderedSentence("claim", text, tuple(ids))


def checker(table=None, nli=None, **kw):
    return FaithfulnessChecker(nli or FakeNLI(), FakeTranslator(table), **kw)


def test_a_clean_english_reply_passes_without_translating():
    tr = FakeTranslator()
    r = FaithfulnessChecker(FakeNLI(), tr).check(reply("en-IN", cs("The settlement amount is 8200 rupees.", "c1")), [C1, C2])
    assert r.ok and tr.calls == [] and r.sentences[0].supported_by == "c1"


def test_an_extra_sentence_the_claims_do_not_support_is_blocked():
    rep = reply("en-IN", cs("The settlement amount is 8200 rupees.", "c1"), cs("It will be fixed soon.", "c2"))
    r = checker().check(rep, [C1, C2])
    assert not r.ok and [s.index for s in r.failed] == [1] and "not_entailed_by_any_verified_claim" in r.failed[0].reasons[0]


def test_a_kannada_number_word_that_changes_the_amount_is_caught():
    rep = reply("kn-IN", cs("ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಮೊತ್ತ ಮೂರು ಸಾವಿರ ರೂಪಾಯಿ.", "c1"))
    r = checker().check(rep, [C1, C2])
    assert not r.ok and r.failed[0].reasons[0].startswith("number_not_in_cited_claims: ") and r.failed[0].back_translation is None


def test_the_right_amount_written_in_digits_in_kannada_passes_the_number_check():
    text = "ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಮೊತ್ತ 8200 ರೂಪಾಯಿ."
    r = checker({text: "The settlement amount is 8200 rupees."}).check(reply("kn-IN", cs(text, "c1")), [C1, C2])
    assert r.ok, r.sentences


def test_known_limitation_kannada_compound_number_words_are_falsely_blocked_never_leaked():
    # The number parser (#42) reads "ಎಂಟು ಸಾವಿರದ ಇನ್ನೂರು" (8,200) as 8000, so a correct sentence is BLOCKED. That is the safe direction.
    # The renderer is therefore told to write numbers as digits (see render.SYSTEM), which keeps number words out of replies.
    from core.gate import render
    assert "DIGITS (0-9)" in render.SYSTEM
    text = "ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಮೊತ್ತ ಎಂಟು ಸಾವಿರದ ಇನ್ನೂರು ರೂಪಾಯಿ."
    r = checker({text: "The settlement amount is 8200 rupees."}).check(reply("kn-IN", cs(text, "c1")), [C1, C2])
    assert not r.ok and r.failed[0].reasons[0].startswith("number_not_in_cited_claims")


def test_a_number_that_appears_only_after_back_translation_is_caught():
    text = "ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಬಾಕಿ ಇದೆ."
    r = checker({text: "The settlement amount is 5000 rupees."}).check(reply("kn-IN", cs(text, "c1")), [C1, C2])
    assert not r.ok and "after_back_translation" in r.failed[0].reasons[0]


def test_fixed_phrases_are_exempt_and_a_faked_phrase_is_not():
    rep = reply("en-IN", RenderedSentence("phrase", "Thank you.", (), "thank_you"), cs("The settlement amount is 8200 rupees.", "c1"))
    assert checker().check(rep, [C1]).ok
    # a claim sentence that merely RESEMBLES a phrase is still checked as a claim
    sneaky = reply("en-IN", cs("Your money is guaranteed.", "c1"))
    assert not checker().check(sneaky, [C1]).ok


def test_citing_a_claim_the_sentence_does_not_express_is_not_trusted_but_a_true_sentence_still_passes():
    swapped = reply("en-IN", cs("The settlement status is PENDING.", "c1"))                 # states c2's fact, cites c1
    r = checker().check(swapped, [C1, C2])
    assert r.ok and r.sentences[0].supported_by == "c2"


def test_a_sentence_that_combines_two_claims_is_supported_by_the_pair():
    both = reply("en-IN", cs("The settlement amount is 8200 rupees and the status is PENDING.", "c1", "c2"))
    r = checker().check(both, [C1, C2])
    assert r.ok and r.sentences[0].supported_by == "joined"
    assert not checker().check(reply("en-IN", cs("The settlement amount is 8200 rupees and the status is PENDING.", "c1")), [C1, C2]).ok     # cites only c1: its number set differs? no: text words
    # (the second assertion documents that PENDING is not in c1's numbers but IS entailed by c2: ok when c2 is verified)


def test_a_sentence_citing_an_unverified_claim_fails():
    r = checker().check(reply("en-IN", cs("The settlement will arrive tomorrow.", "c3")), [C1, C2])     # c3 was blocked, so it is not in the verified list
    assert not r.ok and r.failed[0].reasons == ("cites_unverified_claim",)


def test_the_premise_is_the_verified_claim_and_the_hypothesis_is_the_back_translation():
    nli = FakeNLI()
    text = "ಬಾಕಿ ಇದೆ"
    checker({text: "The settlement status is PENDING."}, nli).check(reply("kn-IN", cs(text, "c2")), [C2])
    assert nli.calls[0] == ("The settlement status is PENDING.", "The settlement status is PENDING.")
    nli2 = FakeNLI()
    checker({text: "It is pending."}, nli2).check(reply("kn-IN", cs(text, "c2")), [C1, C2])
    assert {p for p, _ in nli2.calls} == {C1.text, C2.text} and all(h == "It is pending." for _, h in nli2.calls)


class Emb:
    def __init__(self, same):
        self.same = same

    def encode(self, texts, kind="passage"):
        return np.array([[1.0, 0.0], [1.0, 0.0] if self.same else [0.0, 1.0]], dtype=np.float32)


def test_the_indic_similarity_floor_is_a_second_independent_signal_and_english_skips_it():
    text = "ಬಾಕಿ ಇದೆ"
    tr = {text: "The settlement status is PENDING."}
    assert checker(tr, embedder=Emb(True), similarity_floor=0.5).check(reply("kn-IN", cs(text, "c2")), [C2]).ok
    low = checker(tr, embedder=Emb(False), similarity_floor=0.5).check(reply("kn-IN", cs(text, "c2")), [C2])
    assert not low.ok and low.failed[0].reasons[0].startswith("low_similarity") and low.failed[0].similarity == 0.0
    assert checker(embedder=Emb(False)).check(reply("en-IN", cs("The settlement status is PENDING.", "c2")), [C2]).ok


def test_translation_splits_long_text_at_the_limit_and_maps_language_codes():
    long = " ".join(f"Sentence number {i} is here." for i in range(120))
    parts = SarvamTranslator.split(long)
    assert len(parts) >= 4 and all(len(p) <= 900 for p in parts) and " ".join(parts).split() == long.split()
    assert all(len(p) <= 900 for p in SarvamTranslator.split("x" * 2500))
    seen = []

    def handler(req):
        b = json.loads(req.content)
        seen.append((b["source_language_code"], b["target_language_code"], len(b["input"])))
        return httpx.Response(200, json={"translated_text": "EN:" + b["input"][:3]})

    cfg = load_config()
    tr = SarvamTranslator(SarvamHTTP("k", cfg, httpx.Client(transport=httpx.MockTransport(handler), base_url=cfg["base_url"])))
    assert tr.to_english("ಬಾಕಿ ಇದೆ", "kn-IN") == "EN:" + "ಬಾಕಿ ಇದೆ"[:3] and tr.to_english("paisa nahi aaya", "hinglish").startswith("EN:")
    assert seen[0][:2] == ("kn-IN", "en-IN") and seen[1][:2] == ("auto", "en-IN")
    assert len(SarvamTranslator(SarvamHTTP("k", cfg, httpx.Client(transport=httpx.MockTransport(handler), base_url=cfg["base_url"]))).to_english(long, "hi-IN").split("EN:")) >= 5


# ================================================================== #49 compliance
@pytest.mark.parametrize("q", ["are you an AI?", "Are you a bot", "am I talking to a human?", "is this a robot", "kya aap robot ho", "kya aap AI ho?",
                               "kya tum insaan ho", "क्या आप एआई हैं", "क्या आप इंसान हैं?", "ನೀವು ಎಐ ಆ", "ಇದು ಬಾಟ್ ಆ?", "ನೀವು ಮನುಷ್ಯರಾ"])
def test_ai_questions_are_recognised_in_all_languages(q):
    assert is_ai_question(q), q


@pytest.mark.parametrize("q", ["mera soundbox band hai", "kal ka settlement nahi aaya", "I want a person to call me", "robot vacuum kharidna hai", "ನನ್ನ ಹಣ ಬಂದಿಲ್ಲ", "मेरा पैसा नहीं आया", ""])
def test_ordinary_messages_are_not_ai_questions(q):
    assert not is_ai_question(q), q


@pytest.mark.parametrize("lang", ["en-IN", "hi-IN", "hinglish", "kn-IN"])
def test_first_contact_carries_identity_and_the_pin_otp_line_in_the_merchants_language(lang):
    c = ComplianceChecker(PH)
    body = [RenderedSentence("claim", "x", ("c1",))]
    out = c.apply(body, lang, first_contact=True, asked_ai=False)
    assert [s.phrase_id for s in out[:2]] == ["identity", "pin_otp"] and out[2:] == body
    assert out[0].text == PH.text("identity", lang) and out[1].text == PH.text("pin_otp", lang)
    assert c.missing(out, lang, first_contact=True, asked_ai=False) == [] and c.missing(body, lang, first_contact=True, asked_ai=False) == ["identity", "pin_otp"]


@pytest.mark.parametrize("lang", ["en-IN", "hi-IN", "hinglish", "kn-IN"])
def test_asked_if_it_is_an_ai_the_answer_is_yes_in_that_language_and_comes_first(lang):
    c = ComplianceChecker(PH)
    out = c.apply([RenderedSentence("claim", "x", ("c1",))], lang, first_contact=False, asked_ai=True)
    assert out[0].phrase_id == "ai_yes" and out[0].text == PH.text("ai_yes", lang)


def test_compliance_lines_are_not_duplicated_and_not_added_when_not_needed():
    c = ComplianceChecker(PH)
    again = c.apply([RenderedSentence("phrase", PH.text("identity", "en-IN"), (), "identity")], "en-IN", first_contact=True, asked_ai=False)
    assert [s.text for s in again].count(PH.text("identity", "en-IN")) == 1
    plain = [RenderedSentence("claim", "x", ("c1",))]
    assert c.apply(plain, "en-IN", first_contact=False, asked_ai=False) == plain


def test_reply_language_is_judged_by_script():
    assert language_ok("Your settlement is pending.", "en-IN") and language_ok("Aapka settlement pending hai.", "hinglish")
    assert language_ok("आपका सेटलमेंट बाकी है।", "hi-IN") and language_ok("ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಬಾಕಿ ಇದೆ.", "kn-IN")
    assert not language_ok("Your settlement is pending.", "kn-IN") and not language_ok("ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಬಾಕಿ ಇದೆ.", "hi-IN")
    assert not language_ok("आपका सेटलमेंट बाकी है।", "en-IN") and not language_ok("आपका सेटलमेंट बाकी है।", "hinglish")


def test_the_phrase_table_is_complete_and_flagged_for_native_review():
    assert PH.review_status == "DRAFT_UNREVIEWED"
    assert set(PH.ids("compliance")) == {"identity", "pin_otp", "ai_yes"} and set(PH.ids("control")) == {"clarify", "handoff"}
    assert PH.is_phrase_text("paytm never asks for your pin or otp") and not PH.is_phrase_text("Your money is guaranteed")
    assert canon("Namaste.") == canon("  namaste ")
