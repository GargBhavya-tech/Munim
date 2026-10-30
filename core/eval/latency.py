"""Latency measurement. Allowed to read real time; nothing else in the repo is."""
from __future__ import annotations

import time
from contextlib import contextmanager


class Stopwatch:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0


@contextmanager
def timed(sink: dict, key: str):
    sw = Stopwatch()
    try:
        yield sw
    finally:
        sink[key] = sw.elapsed_ms()


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100). None for an empty list."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# ---- #145: per-stage accounting, intervals, report, live mode ---------------------------------------------------------------------
import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

STAGES = ("stt", "triage", "decide", "tools", "render", "verify", "back_translate", "tts")
OUT_PATH = Path(__file__).resolve().parent.parent.parent / "eval" / "out" / "latency_cost.json"
LIVE_OUT_PATH = OUT_PATH.with_name("latency_cost_live.json")


def turn_total(stages: dict) -> float:
    """Sum of the recorded stages of one turn (ms). Unknown stage names are an error, not silently ignored."""
    bad = set(stages) - set(STAGES)
    if bad:
        raise ValueError(f"unknown stages: {sorted(bad)}")
    return float(sum(stages.values()))


def percentile_ci(values: list[float], p: float, confidence: float = 0.95) -> tuple[float, float] | None:
    """Distribution-free order-statistic interval for the p-th percentile (binomial; no resampling, so deterministic).

    Returns (lo, hi) taken from sorted values; None for an empty list. Small samples give the full range.
    """
    n = len(values)
    if n == 0:
        return None
    xs = sorted(values)
    q = p / 100.0
    alpha = (1 - confidence) / 2

    def cdf(k: int) -> float:                  # P(Binomial(n, q) <= k)
        return sum(math.comb(n, i) * q**i * (1 - q) ** (n - i) for i in range(0, k + 1))

    lo_i = 0
    for k in range(n):
        if cdf(k) <= alpha:
            lo_i = k
        else:
            break
    hi_i = n - 1
    for k in range(n):
        if cdf(k) >= 1 - alpha:
            hi_i = k
            break
    return xs[lo_i], xs[hi_i]


def summarize(values: list[float]) -> dict:
    out: dict = {"n": len(values)}
    for p in (50, 90, 95, 99):
        out[f"p{p}"] = percentile(values, p)
    out["p50_ci95"] = percentile_ci(values, 50)
    out["p95_ci95"] = percentile_ci(values, 95)
    return out


def stage_report(turns: list[dict]) -> dict:
    """turns: list of {stage: ms}. Per-stage and whole-turn percentiles; absent stages (e.g. text turn has no stt) are skipped."""
    report = {s: summarize([t[s] for t in turns if s in t]) for s in STAGES if any(s in t for t in turns)}
    report["total"] = summarize([turn_total(t) for t in turns])
    return report


def run_live(client, n: int = 10, *, sleep=time.sleep, min_gap_s: float = 1.0, language: str = "en-IN") -> dict:
    """Real chat + TTS + STT calls via a SarvamHTTP-shaped client. Prompts carry a unique nonce so no cache can serve them."""
    turns: list[dict] = []
    usage: list[dict] = []
    errors: list[str] = []
    for i in range(n):
        stages: dict = {}
        try:
            with timed(stages, "decide"):
                res = client.chat([{"role": "user", "content": f"Reply with one short sentence about today's settlement. nonce={i}-{os.getpid()}"}])
            usage.append(dict(res.usage or {}))
            text = (res.text or "Settlement is on time.")[:200]
            with timed(stages, "tts"):
                audio = client.tts(text, language)
            with timed(stages, "stt"):
                client.stt(audio, language)
            turns.append(stages)
        except Exception as e:                      # record and continue; a failed call is data, never a key leak
            errors.append(type(e).__name__)
        if i < n - 1:
            sleep(min_gap_s)
    return {"mode": "live", "requested": n, "ok": len(turns), "errors": errors, "stages": stage_report(turns) if turns else {},
            "usage": usage}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.eval.latency")
    ap.add_argument("--live", action="store_true", help="real Sarvam calls (needs SARVAM_API_KEY)")
    ap.add_argument("-n", type=int, default=10)
    args = ap.parse_args(argv)
    if not args.live:
        print("offline mode: use core.eval.cost.write_report(turns, cases); pass --live for real calls")
        return 0
    if not os.environ.get("SARVAM_API_KEY"):
        print("SARVAM_API_KEY is not set; live mode skipped", file=sys.stderr)
        return 2
    from core.llm.sarvam_http import SarvamHTTP
    result = run_live(SarvamHTTP(), args.n)
    LIVE_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_OUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"live: {result['ok']}/{result['requested']} ok -> {LIVE_OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
