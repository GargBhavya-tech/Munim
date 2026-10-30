"""#144 noisy-speech set. Offline: synthetic carrier + synthetic noise + seeded ASR-error channel. Real gate, real parser, real intents."""
import io
import wave

import numpy as np
import pytest

from core.eval import noisy
from core.eval.noisy import (SNR_LEVELS, SNR_TOLERANCE_DB, clip_rng, corrupt, heard_proposal, load_corpus, make_audio, mix_at_snr, power,
                             read_wav, run_noisy_eval, shop_noise, snr_db)
from core.voice.readback import ReadBackGate


@pytest.fixture
def gate(world):
    return ReadBackGate(world.conn, world.clock, world.registry, ledger=world.ledger)


@pytest.fixture
def world(db_path, clock):
    from tests.world import World
    return World(db_path, clock, {})


def test_corpus_is_40_clips_across_three_languages_with_known_intents():
    c = load_corpus()
    assert len(c) == 40 and {x["lang"] for x in c} == {"hi", "kn", "hinglish"}
    assert sum(x["lang"] == "kn" for x in c) == 13 and sum(x["lang"] == "hi" for x in c) == 13
    assert sum("amount_paise" in x for x in c) == 6 and sum(bool(x.get("spoken_number")) for x in c) == 3


def test_every_clean_clip_resolves_to_its_intent_and_amount():
    for c in load_corpus():
        assert noisy.top_intent(c["text"], c["lang"]) == c["intent"], c["id"]
        if "amount_paise" in c:
            assert noisy.intended_proposal(c)[1]["amount_paise"] == c["amount_paise"] == heard_proposal(c["text"], c["lang"])[1]["amount_paise"]


@pytest.mark.parametrize("snr", SNR_LEVELS)
def test_snr_is_verified_numerically_on_the_written_audio_of_every_clip(snr):
    """Re-measure from the WAV bytes: the carrier and noise are regenerated, the mix is decoded, and noise = decoded - signal."""
    for c in load_corpus():
        wav, achieved = make_audio(c, snr, 144)
        assert abs(achieved - snr) < SNR_TOLERANCE_DB, c["id"]
        sig = noisy.carrier(len(c["text"].split()), noisy.SAMPLE_RATE, clip_rng(144, c["id"], "any", "carrier"))
        mixed, sr = read_wav(wav)
        assert sr == 16000 and len(mixed) == len(sig)
        k = float(np.dot(mixed, sig) / np.dot(sig, sig))     # the clipping guard may scale both by a common factor
        residual = mixed - k * sig
        assert abs(snr_db(k * sig, residual) - snr) < 0.6, c["id"]   # independent re-measure from the 16-bit WAV by projection: the noise is not exactly orthogonal to the carrier, so it is looser than `achieved`


def test_snr_mixing_is_not_a_fixed_dbfs_level():
    """The named mistake: scaling noise to a dBFS level gives a different SNR for a quiet and a loud signal. Ours does not."""
    rng = np.random.default_rng(0)
    noise = shop_noise(16000, 16000, rng)
    for amp in (0.05, 0.4):
        sig = (amp * np.sin(np.arange(16000) / 7.0)).astype(np.float32)
        _, a = mix_at_snr(sig, noise, 10.0)
        assert a == pytest.approx(10.0, abs=1e-3)
    wrong = 0.2 * noise                                       # a fixed-level noise: SNR depends on the signal
    assert abs(snr_db(0.05 * np.sin(np.arange(16000) / 7.0), wrong) - snr_db(0.4 * np.sin(np.arange(16000) / 7.0), wrong)) > 10


def test_generator_is_deterministic_for_a_seed_and_differs_across_seeds():
    c = load_corpus()[8]
    a1, a2, b = make_audio(c, 10, 144), make_audio(c, 10, 144), make_audio(c, 10, 7)
    assert a1[0] == a2[0] and a1[0] != b[0]
    t1 = corrupt(c["text"], 5, clip_rng(144, c["id"], 5, "asr"))
    assert t1 == corrupt(c["text"], 5, clip_rng(144, c["id"], 5, "asr"))
    texts = {corrupt(c["text"], 5, clip_rng(s, c["id"], 5, "asr")) for s in range(40)}
    assert len(texts) > 5


def test_corruption_rises_with_noise_and_changes_numbers_in_both_digits_and_words():
    corpus = load_corpus()
    changed = {s: sum(corrupt(c["text"], s, clip_rng(1, c["id"], s, "asr")) != c["text"] for c in corpus) for s in SNR_LEVELS}
    assert changed[20] < changed[10] < changed[5]
    digits = [c for c in corpus if c["text"].split()[0].isdigit() and "amount_paise" in c]
    seen = set()
    for s in range(300):
        for c in digits[:1]:
            t = corrupt(c["text"], 5, clip_rng(s, c["id"], 5, "asr"))
            h = heard_proposal(t, c["lang"])[1]
            if h:
                seen.add(h["amount_paise"])
    assert 200000 in seen and len(seen) > 3 and all(p % 100 == 0 for p in seen)
    word = next(c for c in corpus if c["id"] == "N10")
    amounts = {(heard_proposal(corrupt(word["text"], 5, clip_rng(s, "N10", 5, "asr")), "hinglish")[1] or {}).get("amount_paise") for s in range(300)}
    assert 300000 in amounts          # "do hazaar" heard as "teen hazaar": a number spoken in words, misheard


def test_clean_run_is_perfect_and_noisy_accuracy_falls_monotonically(gate):
    rep = run_noisy_eval(gate)
    by = rep["by_snr"]
    assert by["clean"]["intent_accuracy"] == 1.0 and by["clean"]["confirmed_correct"] == 6 + 0 + 3
    assert by["20"]["intent_accuracy"] >= by["10"]["intent_accuracy"] >= by["5"]["intent_accuracy"]
    assert by["5"]["intent_accuracy"] < by["clean"]["intent_accuracy"]
    assert rep["clips"] == 40 and rep["label"] == "SIMULATED"
    assert all(by[str(s)]["max_snr_error_db"] < SNR_TOLERANCE_DB for s in SNR_LEVELS)


def test_misheard_actions_executed_is_zero_across_seeds(gate):
    """The ticket's test. Several seeds, so the zero is not luck of one draw; the sets must actually contain misheard proposals."""
    mis = 0
    for seed in (144, 1, 2, 3, 4):
        rep = run_noisy_eval(gate, seed=seed)
        assert rep["misheard_actions_executed"] == 0
        mis += sum(v["misheard_proposals"] for v in rep["by_snr"].values())
    assert mis >= 5, "no misheard proposals were generated: the zero would prove nothing"


def test_report_is_reproducible_and_flags_what_needs_real_audio(gate):
    a, b = run_noisy_eval(gate), run_noisy_eval(gate)
    assert a == b
    assert any("real busy-place" in x for x in a["needs_real_audio_and_live_key"])
    assert "not measured" in a["assumptions"]["note"]


def test_mutation_guard_a_gate_that_confirms_everything_executes_misheard_actions(gate, monkeypatch):
    """Proves the zero depends on the read-back: with the yes/no check broken, misheard actions DO get executed."""
    monkeypatch.setattr(ReadBackGate, "_classify", staticmethod(lambda text, slot: "YES"))
    rep = run_noisy_eval(gate)
    assert rep["misheard_actions_executed"] > 0


def test_a_hold_is_never_skipped_so_nothing_misheard_goes_unprompted(gate):
    rep = run_noisy_eval(gate)
    assert all(v["misheard_executed"] == 0 for v in rep["by_snr"].values())
    assert sum(v["proposals"] for v in rep["by_snr"].values()) == len(rep["turns"])


def test_no_wall_clock_or_network_in_the_module():
    src = open(noisy.__file__, encoding="utf-8").read()
    for bad in ("datetime.now", "time.time", "requests", "httpx", "urllib", "import random", "sarvam"):
        assert bad not in src.replace("Sarvam STT", ""), bad
