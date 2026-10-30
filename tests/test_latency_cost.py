import json

import pytest

from core.eval import cost, latency
from core.eval.cost import CaseUsage


def test_percentile_exact_values():
    xs = [10, 20, 30, 40, 50]
    assert latency.percentile(xs, 50) == 30
    assert latency.percentile(xs, 90) == pytest.approx(46)
    assert latency.percentile(xs, 99) == pytest.approx(49.6)
    assert latency.percentile(xs, 0) == 10 and latency.percentile(xs, 100) == 50
    assert latency.percentile([], 50) is None


def test_percentile_ci_brackets_and_orders():
    xs = list(range(1, 101))
    lo, hi = latency.percentile_ci(xs, 50)
    assert (lo, hi) == (40, 61)                  # binomial order-statistic interval, n=100, median, 95%
    assert lo <= latency.percentile(xs, 50) <= hi
    assert latency.percentile_ci([5], 95) == (5, 5)
    assert latency.percentile_ci([], 95) is None
    lo95, hi95 = latency.percentile_ci(xs, 95)
    assert hi95 == 100 and lo95 >= 90


def test_turn_total_and_unknown_stage_rejected():
    assert latency.turn_total({"stt": 100.0, "decide": 900.5, "tts": 50}) == 1050.5
    with pytest.raises(ValueError):
        latency.turn_total({"stt": 1, "bogus": 2})


def test_stage_report_skips_absent_stages_and_totals():
    turns = [{"decide": 100, "verify": 10}, {"decide": 200, "verify": 20, "stt": 50}]
    r = latency.stage_report(turns)
    assert r["decide"]["n"] == 2 and r["decide"]["p50"] == 150
    assert r["stt"]["n"] == 1
    assert "tts" not in r
    assert r["total"]["p50"] == pytest.approx((110 + 270) / 2)


def test_case_cost_integer_paise_exact():
    u = CaseUsage(tokens_in=32_000, tokens_out=3_400, stt_seconds=48, tts_chars=1_200, translate_chars=1_200)
    # 32000*0.02 + 3400*0.06 + 48*8 + 1200*0.3 + 1200*0.2 = 640 + 204 + 384 + 360 + 240
    assert cost.case_cost_paise(u) == 1828
    assert isinstance(cost.case_cost_paise(u), int)


def test_rounding_half_up_not_truncation():
    assert cost.case_cost_paise(CaseUsage(tokens_in=25)) == 1      # 0.5 paise rounds up
    assert cost.case_cost_paise(CaseUsage(tokens_in=24)) == 0      # 0.48 rounds down


def test_escalation_adds_human_time():
    base = cost.case_cost_paise(CaseUsage(tokens_in=1000))
    assert cost.case_cost_paise(CaseUsage(tokens_in=1000, escalated=True)) == base + 6000


def test_cost_per_resolved_counts_unresolved_spend():
    cases = [CaseUsage(tokens_in=5000), CaseUsage(tokens_in=5000, resolved=False)]   # 100 paise each
    r = cost.cost_report(cases)
    assert r["total_paise"] == 200 and r["resolved"] == 1
    assert r["cost_per_resolved_case_paise"] == 200
    assert cost.cost_report([CaseUsage(resolved=False)])["cost_per_resolved_case_paise"] is None


def test_human_baseline_matches_bible():
    assert cost.HUMAN_COST_PAISE == {"low": 2400, "mid": 6000, "high": 15000}


def test_report_written_and_labelled(tmp_path):
    p = tmp_path / "o" / "latency_cost.json"
    cost.write_report([{"decide": 120.0}], [CaseUsage(tokens_in=1000)], p)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["tier_c_cost"]["cost_per_resolved_case_paise"] == 20
    assert "ASSUMPTION" in d["tier_c_cost"]["label"]
    assert d["latency_ms"]["decide"]["p50"] == 120.0


class _Chat:
    text, usage = "ok sentence", {"prompt_tokens": 10, "completion_tokens": 5}


class FakeClient:
    def __init__(self, fail_on=None):
        self.prompts, self.calls, self.fail_on = [], 0, fail_on

    def chat(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        self.calls += 1
        if self.fail_on == self.calls:
            raise RuntimeError("boom sk-secret")
        return _Chat()

    def tts(self, text, lang):
        return b"wav"

    def stt(self, wav, lang):
        return {"transcript": "x"}


def test_live_mode_fake_transport_unique_prompts_and_rate_limit():
    fc, sleeps = FakeClient(), []
    r = latency.run_live(fc, 4, sleep=sleeps.append, min_gap_s=0.5)
    assert r["ok"] == 4 and len(set(fc.prompts)) == 4        # nonce defeats any cache
    assert sleeps == [0.5, 0.5, 0.5]
    assert set(r["stages"]) == {"decide", "tts", "stt", "total"}
    assert r["usage"][0] == {"prompt_tokens": 10, "completion_tokens": 5}


def test_live_failure_recorded_without_leaking_message():
    r = latency.run_live(FakeClient(fail_on=2), 3, sleep=lambda s: None)
    assert r["ok"] == 2 and r["errors"] == ["RuntimeError"]
    assert "sk-secret" not in json.dumps(r)


def test_live_cli_skips_without_key(monkeypatch, capsys):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    assert latency.main(["--live"]) == 2
