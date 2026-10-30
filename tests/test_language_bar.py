"""#126 / E12: a language is switched on only by a passing per-language report (STT, TTS, translate, false-block <= 5% on >= 30 labelled replies)."""
import pytest

from core.audit.ledger import Ledger
from core.voice.language_bar import LabelledReply, LanguageNotReady, LanguageRegistry, candidates, evaluate, load_config, load_labelled

CFG = load_config()
GOOD = {"stt": "spike-1", "tts": "spike-2", "translate": "spike-3"}
NON_BASELINE = [c for c in candidates(CFG) if c not in CFG["baseline"]]


def labelled(n_grounded=20, n_ungrounded=10):
    return ([LabelledReply(f"g{i}", f"grounded {i}", True) for i in range(n_grounded)] + [LabelledReply(f"u{i}", f"ungrounded {i}", False) for i in range(n_ungrounded)])


def gate(block_grounded=(), send_ungrounded=()):
    """A stand-in gate: sends every grounded reply except the ones named, blocks every ungrounded reply except the ones named."""
    return lambda r: (r.reply_id not in block_grounded) if r.grounded else (r.reply_id in send_ungrounded)


@pytest.fixture
def reg(conn, clock):
    return LanguageRegistry(conn, clock, Ledger(conn, clock))


def test_there_are_eleven_candidates_and_the_baseline_is_among_them():
    assert len(candidates(CFG)) == 11 and set(CFG["baseline"]) - {"hinglish"} <= set(candidates(CFG))


@pytest.mark.parametrize("code", NON_BASELINE)
def test_a_language_without_its_labelled_set_is_off_and_cannot_be_enabled(code, reg):
    assert load_labelled(code) == [] and not reg.is_enabled(code)
    report = evaluate(code, GOOD, [], gate(), CFG)                                    # every other check passes; the set is missing
    assert not report["passes"] and "only 0 labelled replies" in report["why_not"][0]
    with pytest.raises(LanguageNotReady):
        reg.enable(report)
    assert not reg.is_enabled(code)


def test_a_passing_report_enables_the_language_and_is_ledgered(reg, conn):
    report = evaluate("ta-IN", GOOD, labelled(), gate(), CFG)
    assert report["passes"] and report["false_block_rate"] == 0.0 and report["labelled"] == {"n": 30, "grounded": 20, "ungrounded": 10}
    reg.enable(report)
    assert reg.is_enabled("ta-IN")
    st = {s["code"]: s for s in reg.status()}
    assert st["ta-IN"]["report"] == "passing" and st["ta-IN"]["report_detail"]["enabled_at"]
    assert st["te-IN"]["enabled"] is False
    entries = [e.payload for e in reg.ledger.entries() if "language_enabled" in e.payload]
    assert entries == [{"language_enabled": "ta-IN", "false_block_rate": 0.0, "labelled": {"n": 30, "grounded": 20, "ungrounded": 10}}]


def test_the_false_block_boundary_is_exactly_five_percent():
    ok = evaluate("ta-IN", GOOD, labelled(20, 10), gate(block_grounded={"g0"}), CFG)               # 1 of 20 = 5.0%
    assert ok["false_block_rate"] == 0.05 and ok["passes"]
    bad = evaluate("ta-IN", GOOD, labelled(20, 10), gate(block_grounded={"g0", "g1"}), CFG)        # 2 of 20 = 10%
    assert not bad["passes"] and "false-block rate 10.0%" in bad["why_not"][0]


def test_a_single_leak_fails_the_language():
    r = evaluate("ta-IN", GOOD, labelled(), gate(send_ungrounded={"u3"}), CFG)
    assert not r["passes"] and r["leaks"] == 1 and "1 ungrounded replies were sent" in r["why_not"][0]


@pytest.mark.parametrize("missing", ["stt", "tts", "translate"])
def test_each_speech_and_translation_check_is_required(missing):
    checks = {**GOOD, missing: None}
    r = evaluate("ta-IN", checks, labelled(), gate(), CFG)
    assert not r["passes"] and r["why_not"] == [f"{missing} has no passing check"]


def test_twenty_nine_labelled_replies_is_not_enough():
    r = evaluate("ta-IN", GOOD, labelled(19, 10), gate(), CFG)
    assert not r["passes"] and "only 29 labelled replies (need 30)" in r["why_not"][0]


def test_a_set_with_only_one_kind_of_reply_cannot_pass():
    assert not evaluate("ta-IN", GOOD, labelled(30, 0), gate(), CFG)["passes"]
    assert not evaluate("ta-IN", GOOD, labelled(0, 30), gate(), CFG)["passes"]


def test_a_hand_edited_passing_flag_without_the_set_is_still_refused(reg):
    forged = {"code": "ta-IN", "passes": True, "why_not": [], "labelled": {"n": 3, "grounded": 2, "ungrounded": 1}, "false_block_rate": 0.0}
    with pytest.raises(LanguageNotReady):
        reg.enable(forged)


def test_baseline_languages_stay_on_but_are_reported_as_having_no_report(reg):
    st = {s["code"]: s for s in reg.status()}
    assert st["hi-IN"]["enabled"] and st["hi-IN"]["report"] == "none yet (baseline language)"
    assert st["hinglish"]["enabled"]                                                                 # not a candidate, still listed
    with pytest.raises(LanguageNotReady):
        reg.disable("kn-IN")


def test_disabling_removes_the_language(reg):
    reg.enable(evaluate("ta-IN", GOOD, labelled(), gate(), CFG))
    reg.disable("ta-IN")
    assert not reg.is_enabled("ta-IN")


def test_an_unknown_language_is_not_a_candidate():
    with pytest.raises(LanguageNotReady):
        evaluate("xx-IN", GOOD, labelled(), gate(), CFG)
