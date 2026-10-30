"""Noise mixing at a target SNR (not a target dBFS), with the achieved SNR measured, never assumed."""
from __future__ import annotations

import io
import wave

import numpy as np


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Mono float32 in [-1, 1] and the sample rate. Multi-channel audio is averaged."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        sr, ch = w.getframerate(), w.getnchannels()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        raw = raw.reshape(-1, ch).mean(axis=1)
    return raw, sr


def write_wav(samples: np.ndarray, sr: int) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def power(x: np.ndarray) -> float:
    return float(np.mean(np.square(x.astype(np.float64))))


def snr_db(signal: np.ndarray, noise: np.ndarray) -> float:
    return 10.0 * np.log10(power(signal) / power(noise))


def shop_noise(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Synthetic shop/street noise: low rumble, broadband hiss, babble-like modulated bands and clatter bursts."""
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    rumble = np.fft.irfft(spec * (freqs < 300) * 1.0, n)
    hiss = np.fft.irfft(spec * (freqs > 2000) * 0.15, n)
    babble = np.zeros(n)
    t = np.arange(n) / sr
    for f in (180, 260, 340, 520, 780):
        env = 0.5 * (1 + np.sin(2 * np.pi * rng.uniform(2, 6) * t + rng.uniform(0, 6.28)))
        babble += env * np.fft.irfft(spec * ((freqs > f) & (freqs < f * 1.6)), n)
    clatter = np.zeros(n)
    for _ in range(max(1, int(n / sr))):
        i = int(rng.uniform(0, max(1, n - 800)))
        clatter[i:i + 800] += rng.standard_normal(min(800, n - i)) * np.exp(-np.arange(min(800, n - i)) / 120.0)
    out = rumble + hiss + babble * 0.8 + clatter * 0.3
    return (out / (np.sqrt(power(out)) + 1e-12)).astype(np.float32)


def mix_at_snr(signal: np.ndarray, noise: np.ndarray, target_snr_db: float) -> tuple[np.ndarray, float]:
    """Return (mixed, achieved_snr_db). Noise is scaled so signal/noise power ratio equals the target."""
    if len(noise) < len(signal):
        noise = np.tile(noise, int(np.ceil(len(signal) / len(noise))))
    noise = noise[: len(signal)].astype(np.float64)
    gain = np.sqrt(power(signal) / (power(noise) * 10 ** (target_snr_db / 10.0)))
    scaled = noise * gain
    mixed = signal.astype(np.float64) + scaled
    peak = float(np.max(np.abs(mixed)))
    if peak > 0.95:  # a common scale factor keeps the SNR unchanged
        k = 0.95 / peak
        mixed, signal_out, scaled = mixed * k, signal * k, scaled * k
    else:
        signal_out = signal
    return mixed.astype(np.float32), snr_db(signal_out, scaled)


# ======================================================================================================================
# #144 Noisy-speech set: seeded, offline, deterministic. Two separate halves:
#   AUDIO   synthetic speech-like carrier + synthetic shop noise, mixed at 20/10/5 dB SNR; the achieved SNR is MEASURED per clip.
#   TEXT    a seeded ASR-error channel (word drop, vowel/last-letter confusion, filler, digit and number-word confusion) whose rates
#           depend on the SNR. This is a STAND-IN for a real recogniser. Its rates are ASSUMPTIONS, not measurements of Sarvam.
# What it does NOT do: say anything about real recogniser accuracy. That needs real busy-place recordings plus a live key (see
# eval/noisy/README.md). Nothing here calls a network service or reads the wall clock.
# ======================================================================================================================
import hashlib
from pathlib import Path

import yaml

from core.engine.intents import top_intent
from core.gate.numbers import extract_numbers

CORPUS_PATH = Path(__file__).resolve().parents[2] / "eval" / "noisy" / "corpus.yaml"
SNR_LEVELS = (20, 10, 5)
SAMPLE_RATE = 16_000
WORD_ERROR_RATE = {20: 0.04, 10: 0.12, 5: 0.25}   # ASSUMPTION: stand-in channel, per word. Number tokens fail at twice this rate.
SNR_TOLERANCE_DB = 0.05
FILLERS = ("uh", "hmm", "acha")
NUMBER_CONFUSION = {"do": "teen", "teen": "do", "paanch": "pachaas", "sau": "hazaar", "hazaar": "sau",
                    "दो": "तीन", "तीन": "दो", "हज़ार": "सौ", "हजार": "सौ", "सौ": "हज़ार"}
VOWEL_SWAP = str.maketrans("aeiou", "eiuoa")
INTENT_ACTION = {"REFUND_REQUEST": "create_refund_request", "CLOSURE_REQUEST": "create_closure_request"}   # intents that propose a write


def load_corpus(path: str | Path = CORPUS_PATH) -> list[dict]:
    clips = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["clips"]
    if len({c["id"] for c in clips}) != len(clips):
        raise ValueError("duplicate clip ids")
    return clips


def clip_rng(seed: int, clip_id: str, snr: int | str, stream: str) -> np.random.Generator:
    """Independent, reproducible stream per (seed, clip, SNR, purpose); no global state, no hash() (which is salted per process)."""
    h = hashlib.sha256(f"{seed}|{clip_id}|{snr}|{stream}".encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "big"))


def carrier(n_words: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Speech-LIKE test signal (voiced harmonics under a ~4 Hz syllable envelope). Synthetic stand-in, not speech."""
    n = int((0.45 * n_words + 0.6) * sr)
    t = np.arange(n) / sr
    f0 = rng.uniform(110, 190)
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    env = 0.5 * (1 + np.sin(2 * np.pi * rng.uniform(3.5, 4.5) * t + rng.uniform(0, 6.28)))
    return (sig * env / (np.max(np.abs(sig)) + 1e-12) * 0.5).astype(np.float32)


def make_audio(clip: dict, snr: int, seed: int) -> tuple[bytes, float]:
    """(16-bit WAV bytes, achieved SNR in dB). Achieved SNR is measured on the final signal and noise as mixed."""
    n_words = max(1, len(clip["text"].split()))
    sig = carrier(n_words, SAMPLE_RATE, clip_rng(seed, clip["id"], "any", "carrier"))
    noise = shop_noise(len(sig), SAMPLE_RATE, clip_rng(seed, clip["id"], snr, "noise"))
    mixed, achieved = mix_at_snr(sig, noise, float(snr))
    return write_wav(mixed, SAMPLE_RATE), achieved


def corrupt(text: str, snr: int, rng: np.random.Generator) -> str:
    """Seeded ASR-error channel. Digits in a number change to a different digit; number words swap with a confusable one."""
    p = WORD_ERROR_RATE[snr]
    out: list[str] = []
    for w in text.split():
        is_digits = w.isdigit()
        is_numword = w in NUMBER_CONFUSION
        r = rng.random()
        if is_digits or is_numword:
            if r < min(1.0, 2 * p):
                if is_digits:
                    i = int(rng.integers(len(w)))
                    d = str((int(w[i]) + int(rng.integers(1, 10))) % 10)
                    w = w[:i] + d + w[i + 1:]
                else:
                    w = NUMBER_CONFUSION[w]
        elif r < p / 2:
            continue                                            # word dropped
        elif r < p:
            w = w.translate(VOWEL_SWAP) if w.isascii() else (w[:-1] if len(w) > 2 else w)
        out.append(w)
        if rng.random() < p / 4:
            out.append(FILLERS[int(rng.integers(len(FILLERS)))])
    return " ".join(out)


def heard_proposal(text: str, lang: str) -> tuple[str | None, dict | None]:
    """What the system would propose from a transcript: (action, args) or (None, None). Amounts come from the number parser, in paise."""
    action = INTENT_ACTION.get(top_intent(text, lang) or "")
    if action is None:
        return None, None
    if action == "create_closure_request":
        return action, {"merchant_id": "M-0001"}
    money = [n for n in extract_numbers(text) if n.kind == "money"]
    if not money:
        return action, None                                     # intent heard, amount lost: must ask the merchant to repeat
    return action, {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": int(money[0].value)}


def intended_proposal(clip: dict) -> tuple[str | None, dict | None]:
    action = INTENT_ACTION.get(clip["intent"])
    if action is None:
        return None, None
    if action == "create_closure_request":
        return action, {"merchant_id": "M-0001"}
    return action, {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": clip["amount_paise"]}


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def run_noisy_eval(gate, corpus: list[dict] | None = None, *, seed: int = 144, merchant_id: str = "M-0001") -> dict:
    """Run the whole set offline. `gate` is a real ReadBackGate. Every voice write is read back, because the speech API gives no per-
    transcript confidence and a missing confidence counts as LOW (config/voice.yaml). The simulated merchant answers truthfully:
    "haan" if the read-back matches what they meant, else a correction that carries the right number or a plain "nahi"."""
    corpus = corpus if corpus is not None else load_corpus()
    levels = ("clean",) + tuple(str(s) for s in SNR_LEVELS)
    per: dict[str, dict] = {k: dict(clips=0, intent_ok=0, snr_err=[], write_turns=0, proposals=0, no_amount=0, unprompted_wrong=0,
                                    confirmed_ok=0, confirmed_wrong=0, declined=0, single_prompt=0, misheard_money=0) for k in levels}
    rows = []
    for clip in corpus:
        i_act, i_args = intended_proposal(clip)
        for lv in levels:
            snr = None if lv == "clean" else int(lv)
            heard = clip["text"] if snr is None else corrupt(clip["text"], snr, clip_rng(seed, clip["id"], snr, "asr"))
            s = per[lv]
            s["clips"] += 1
            got = top_intent(heard, clip["lang"])
            s["intent_ok"] += got == clip["intent"]
            if snr is not None:
                _, achieved = make_audio(clip, snr, seed)
                s["snr_err"].append(abs(achieved - snr))
            h_act, h_args = heard_proposal(heard, clip["lang"])
            s["write_turns"] += i_act is not None
            if i_act is not None and h_act == i_act and h_args is None:
                s["no_amount"] += 1                              # asks the merchant to repeat; nothing is held or executed
            if h_act is None or h_args is None:
                continue
            s["proposals"] += 1
            wrong = (h_act, h_args) != (i_act, i_args)
            s["misheard_money"] += wrong
            case_id = f"NZ-{clip['id']}-{lv}"
            hold = gate.review(case_id, merchant_id, h_act, h_args, low_confidence=True, language="hinglish")
            if hold.outcome != "HOLD":
                s["unprompted_wrong"] += wrong                   # would have executed with no read-back
                continue
            if wrong:
                reply = "nahi" if i_act is None else f"nahi {i_args.get('amount_paise', 0) // 100}"
            else:
                reply = "haan"
            res = gate.resolve(case_id, merchant_id, reply)
            s["single_prompt"] += res.status in ("CONFIRMED", "DECLINED")
            if res.status == "CONFIRMED":
                s["confirmed_wrong" if wrong else "confirmed_ok"] += 1
            else:
                s["declined"] += 1
            rows.append({"clip": clip["id"], "snr": lv, "heard": heard, "action": h_act, "args": h_args, "misheard": wrong, "result": res.status})
    by = {}
    for lv, s in per.items():
        by[lv] = {"clips": s["clips"], "intent_accuracy": _rate(s["intent_ok"], s["clips"]), "write_turns": s["write_turns"],
                  "proposals": s["proposals"], "amount_lost_ask_to_repeat": s["no_amount"], "misheard_proposals": s["misheard_money"],
                  "misheard_executed": s["confirmed_wrong"] + s["unprompted_wrong"], "confirmed_correct": s["confirmed_ok"], "not_confirmed": s["declined"],
                  "ask_once_rate": _rate(s["single_prompt"], s["proposals"]),
                  "max_snr_error_db": round(max(s["snr_err"]), 4) if s["snr_err"] else None}
    return {
        "ticket": 144, "label": "SIMULATED", "seed": seed, "clips": len(corpus),
        "by_langs": {lang: sum(c["lang"] == lang for c in corpus) for lang in ("hi", "kn", "hinglish")},
        "by_snr": by,
        "misheard_actions_executed": sum(v["misheard_executed"] for v in by.values()),
        "assumptions": {"word_error_rate": {str(k): v for k, v in WORD_ERROR_RATE.items()},
                        "note": "stand-in ASR channel; rates are assumed, not measured. Audio carrier is synthetic, not speech."},
        "needs_real_audio_and_live_key": ["real busy-place recordings (hi/kn/Hinglish)", "recogniser word/intent accuracy on them (Sarvam STT)",
                                          "whether language_probability is usable as a confidence signal", "open noise corpus (licence check) instead of synthetic shop noise"],
        "turns": rows,
    }


def main(argv: list[str] | None = None) -> int:
    """python -m core.eval.noisy [--seed N] [--out PATH]: builds a throwaway database, runs the offline set, writes the JSON report."""
    import argparse
    import json
    import tempfile

    from core.engine.clock import SimClock
    from core.memory.store import connect, init_schema
    from core.voice.readback import ReadBackGate
    from packs.paytm_merchant.actions import build_registry

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=144)
    ap.add_argument("--out", default=str(CORPUS_PATH.parent / "report.json"))
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "noisy.db")
        init_schema(conn)
        rep = run_noisy_eval(ReadBackGate(conn, SimClock(), build_registry()), seed=a.seed)
        conn.close()
    Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {a.out}: misheard actions executed = {rep['misheard_actions_executed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
