"""#99 turnover estimate and lead scoring: formula first, uncertainty flagged on single-signal estimates, a leakage guard, and AUC on LATER weeks only."""
import numpy as np
import pytest

from core.audit.ledger import Ledger
from jobs.dedupe import DedupeJob
from jobs.scoring import (CONVERSION_WINDOW_DAYS, LeakageError, Logistic, auc, estimate_turnover, features, formula_value, matured, score_ready_leads,
                          synthetic_conversions, walk_forward)


def test_auc_known_values():
    assert auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75
    assert auc([0, 1], [0.2, 0.9]) == 1.0 and auc([1, 0], [0.2, 0.9]) == 0.0
    assert auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5               # ties average out
    assert auc([1, 1, 1], [0.1, 0.2, 0.3]) is None                       # one class: no AUC


def test_a_single_signal_estimate_is_flagged_uncertain_and_wider():
    one = estimate_turnover("40L-1.5Cr", None, None)
    two = estimate_turnover("40L-1.5Cr", "kirana", None)
    four = estimate_turnover("40L-1.5Cr", "kirana", 0.5, 120)
    assert one.uncertain and one.signals == ("gst_slab",) and not two.uncertain and not four.uncertain
    width = lambda t: (t.high_paise - t.low_paise) / t.monthly_paise      # noqa: E731
    assert width(one) > width(two) > width(four)
    assert estimate_turnover(None, None, None) is None                   # no signal at all: no estimate


def test_the_formula_is_volume_times_probabilities_over_cost():
    t = estimate_turnover(">5Cr", "d2c", 0.9)
    big, small = formula_value(t, "d2c", "ai_voice"), formula_value(estimate_turnover("<40L", "salon", 0.1), "salon", "ai_voice")
    assert big > small > 0
    assert formula_value(t, "d2c", "self_serve") > formula_value(t, "d2c", "ai_voice") > formula_value(t, "d2c", "field_rep")   # cheaper channel, higher return per rupee
    assert formula_value(t, "d2c", "ai_voice", p_switch=0.24) == pytest.approx(formula_value(t, "d2c", "ai_voice", p_switch=0.12) * 2, abs=1)


def test_a_row_with_post_conversion_fields_is_refused():
    clean = synthetic_conversions(weeks=1, per_week=5)[0]
    features(clean)
    with pytest.raises(LeakageError):
        features(synthetic_conversions(weeks=1, per_week=5, leak=True)[0])
    with pytest.raises(LeakageError):
        features({**clean, "days_to_activate": 3})


def test_a_leaky_feature_would_have_faked_a_perfect_auc_which_is_why_it_is_refused():
    rows = synthetic_conversions(weeks=10, per_week=80, leak=True)
    pooled = auc([r["converted"] for r in rows], [r["first_settlement_paise"] for r in rows])
    assert pooled == 1.0                                                  # the leak alone separates the classes perfectly
    honest = walk_forward(synthetic_conversions(weeks=10, per_week=80))
    assert 0.55 < honest["auc_later_weeks"] < 0.9                         # the real, modest signal


def test_labels_are_used_only_after_the_conversion_window_has_passed():
    rows = synthetic_conversions(weeks=6, per_week=10)
    lag = -(-CONVERSION_WINDOW_DAYS // 7)
    for w in range(6):
        assert all(r["week"] + lag <= w for r in matured(rows, w))
    assert matured(rows, 0) == [] and {r["week"] for r in matured(rows, 4)} == {0, 1, 2}


def test_walk_forward_reports_only_later_weeks_and_never_trains_on_the_week_it_tests():
    rows = synthetic_conversions(weeks=12, per_week=80)
    r = walk_forward(rows)
    assert min(r["per_week"]) >= r["first_test_week"] == 6
    assert r["tested_rows"] == sum(1 for x in rows if x["week"] in r["per_week"]) and r["tested_rows"] > 300
    assert r["auc_later_weeks"] > 0.6
    # shuffle the labels: with no real signal the same pipeline must fall back to chance, so a good AUC above is not an artefact
    rng = np.random.default_rng(0)
    labels = rng.permutation([x["converted"] for x in rows])
    shuffled = [{**x, "converted": int(l)} for x, l in zip(rows, labels)]
    assert abs(walk_forward(shuffled)["auc_later_weeks"] - 0.5) < 0.08


def test_the_model_recovers_the_direction_of_the_true_effects():
    rows = synthetic_conversions(weeks=12, per_week=120)
    m = Logistic().fit(np.array([features(r) for r in rows]), np.array([r["converted"] for r in rows], float))
    w = dict(zip(("bias", "slab", "ticket", "area_busyness", "confidence", "review", "kirana", "restaurant", "pharmacy"), m.w))
    assert w["slab"] > 0 and w["area_busyness"] > 0 and w["kirana"] > 0 and abs(w["review"]) < abs(w["area_busyness"])


def test_ranked_leads_come_only_from_the_cleared_set_with_uncertainty_and_a_channel(conn, clock):
    assert score_ready_leads(conn, clock) == []                          # nothing is cleared yet: nothing is scored
    DedupeJob(conn, clock, Ledger(conn, clock)).run()
    ranked = score_ready_leads(conn, clock)
    assert len(ranked) > 100 and [s.value for s in ranked] == sorted((s.value for s in ranked), reverse=True)
    cleared = {r["lead_id"] for r in conn.execute("SELECT lead_id FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND provenance_json LIKE '%CLEAR%'")}
    assert {s.lead_id for s in ranked} <= cleared
    assert {s.channel for s in ranked} <= {"field_rep", "ai_voice", "self_serve"}
    assert all(s.uncertain == (len(s.turnover.signals) < 2) for s in ranked)           # the flag is exactly "fewer than two signals"
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE score IS NOT NULL").fetchone()[0] == len(ranked)
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE score IS NOT NULL AND state != 'NEW'").fetchone()[0] == 0     # a held or existing lead is never scored
