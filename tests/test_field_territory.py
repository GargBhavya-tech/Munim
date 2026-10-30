"""#112 territory redesign (balanced by opportunity + service load, not shop count) and seasonal fault forecasts with a backtest (gate E9)."""
import math
import random

import pytest

from field.territory import (Area, area_loads, backtest, fault_history, forecast_month, redesign, staffing, van_stock, _months_before)

# ---------------------------------------------------------------- territories
AREAS = [Area("H1", 9_000_000, 600, 1), Area("H2", 8_000_000, 500, 1)] + [Area(f"L{i}", 500_000, 80, n) for i, n in enumerate((20, 18, 16, 14, 12, 10), 1)]


def test_loads_are_half_opportunity_share_and_half_service_share():
    ls = area_loads([Area("A", 3_000_000, 100), Area("B", 1_000_000, 300)])
    assert ls == {"A": pytest.approx(0.5 * 0.75 + 0.5 * 0.25), "B": pytest.approx(0.5 * 0.25 + 0.5 * 0.75)}
    assert sum(area_loads(AREAS).values()) == pytest.approx(1.0)


def test_balancing_by_load_beats_balancing_by_shop_count():
    by_load, by_shops = redesign(AREAS, 2), redesign(AREAS, 2, by="shops")
    assert sorted(a for t in by_load.territories for a in t) == sorted(a.area_id for a in AREAS)            # every area exactly once
    assert any({"H1", "H2"} <= set(t) for t in by_shops.territories)           # shop count happily puts both big-opportunity areas together
    assert not any({"H1", "H2"} <= set(t) for t in by_load.territories)
    assert by_load.spread < 0.05 and by_shops.spread > 0.25 and by_load.spread * 5 < by_shops.spread


def test_redesign_is_deterministic_and_validates():
    assert redesign(AREAS, 3).territories == redesign(list(reversed(AREAS)), 3).territories
    with pytest.raises(ValueError):
        redesign(AREAS, 0)
    with pytest.raises(ValueError):
        redesign(AREAS, 9)
    assert redesign(AREAS, 1).territories == [sorted(a.area_id for a in AREAS)] and redesign(AREAS, 1).spread == 0


def test_local_improvement_fixes_a_case_the_greedy_start_gets_wrong():
    # loads 7,6,5,4,4: heaviest-first gives 7+4+4=15 against 6+5=11; swapping 7 and 5 reaches 13 and 13
    areas = [Area(str(i), v * 1_000_000, v * 100) for i, v in enumerate((7, 6, 5, 4, 4))]
    r = redesign(areas, 2)
    total = {a.area_id: a.opportunity_paise // 1_000_000 for a in areas}
    assert sorted(sum(total[x] for x in t) for t in r.territories) == [13, 13] and r.spread == pytest.approx(0, abs=1e-9)


# ---------------------------------------------------------------- forecasts
def synth_history(seed=3, years=4, areas=("560001", "560002", "560003")):
    rnd = random.Random(seed)
    season = {6: 1.8, 7: 2.0, 8: 1.9, 9: 1.5, 12: 1.2}            # monsoon and festival faults
    base = {"560001": 20, "560002": 8, "560003": 12}
    out = {}
    for a in areas:
        out[a] = {}
        for y in range(2022, 2022 + years):
            for m in range(1, 13):
                growth = 1 + 0.05 * (y - 2022)
                out[a][f"{y}-{m:02d}"] = max(0, round(base[a] * season.get(m, 1.0) * growth + rnd.uniform(-2, 2)))
    return out


def test_months_before_handles_year_boundaries():
    assert _months_before("2026-01", 1) == "2025-12" and _months_before("2026-03", 12) == "2025-03" and _months_before("2026-02", 14) == "2024-12"


def test_forecast_month_carries_each_earlier_year_forward_by_the_growth():
    s = {f"{y}-{m:02d}": 10 for y in (2023, 2024) for m in range(1, 13)}
    s.update({"2023-07": 40, "2024-07": 44})
    g = (10 * 11 + 44) / (10 * 11 + 40)                                       # latest 12 months over the 12 before
    assert forecast_month(s, "2025-07") == pytest.approx((44 * g + 40 * g ** 2) / 2)
    assert forecast_month({"2025-04": 6, "2025-05": 9}, "2025-06") == pytest.approx(7.5)
    assert forecast_month({}, "2025-06") == 0.0


def test_backtest_reports_mae_and_beats_the_naive_baseline_on_a_seasonal_history():
    hist = synth_history()
    holdout = [f"2025-{m:02d}" for m in range(1, 13)]
    bt = backtest(hist, holdout)
    assert bt.months == holdout and set(bt.per_area) == {"560001", "560002", "560003"}
    assert bt.mae < 3.5                                       # injected noise is +-2 and growth is 5% a year
    assert bt.mae < bt.baseline_mae / 2                       # "same as last month" misses every monsoon jump
    assert bt.mae == pytest.approx(sum(bt.per_area.values()) / 3, abs=1e-3)


def test_the_holdout_never_leaks_into_its_own_forecast():
    hist = synth_history()
    holdout = [f"2025-{m:02d}" for m in range(1, 13)]
    clean = backtest(hist, holdout)
    poisoned = {a: {**s, **{ym: 9999 for ym in holdout}} for a, s in hist.items()}
    assert clean.mae != backtest(poisoned, holdout).mae                         # the actuals differ, as they should
    # the forecasts (not the actuals) must be identical
    past = {k: v for k, v in poisoned["560001"].items() if k < holdout[0]}
    assert forecast_month(past, "2025-07") == forecast_month({k: v for k, v in hist["560001"].items() if k < holdout[0]}, "2025-07")


# ---------------------------------------------------------------- staffing and van stock
def test_staffing_and_van_stock_are_integer_arithmetic_over_the_forecast():
    assert staffing(41.2, 90, 22, 360) == math.ceil(42 * 90 / (22 * 360)) == 1
    assert staffing(300.0, 90, 22, 360) == 4                  # 27000 / 7920 = 3.4 -> 4
    assert staffing(0, 90, 22, 360) == 0
    assert van_stock(41.2, {"soundbox_unit": 0.5, "adapter": 1.0}) == {"adapter": 47, "soundbox_unit": 24}     # 42 * 1.0 * 1.10 = 46.2; 42 * 0.5 * 1.10 = 23.1


def test_fault_history_reads_tickets_by_pincode_and_month(conn):
    pin = conn.execute("SELECT pincode FROM merchants WHERE merchant_id = 'M-0001'").fetchone()[0]
    for i, ts in enumerate(("2026-06-03T10:00:00+05:30", "2026-06-20T10:00:00+05:30", "2026-07-01T10:00:00+05:30")):
        conn.execute("INSERT INTO tickets(ticket_id, merchant_id, type, status, created_at) VALUES (?,?,?,?,?)", (f"TK-T{i}", "M-0001", "DEVICE_FAULT", "OPEN", ts))
    h = fault_history(conn, ("DEVICE_FAULT",))
    assert h[pin] == {"2026-06": 2, "2026-07": 1}
    assert fault_history(conn, ("OTHER",)) == {}
    conn.execute("ALTER TABLE reps ADD COLUMN religion TEXT")
    with pytest.raises(AssertionError):
        fault_history(conn)
