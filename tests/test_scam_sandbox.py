"""The scam sandbox: a claim goes into a throwaway copy of the books, a verdict comes out by code, and the real books are never touched."""
import json
import statistics
from datetime import timedelta

import pytest

from packs.paytm_merchant.scam_sandbox import (CANNOT_TELL, DAY_STRETCH_X, LIKELY_SCAM, OUTLIER_TICKET_X, REAL, Claim, check_claim)
from tests.checkpoint_script import SCRIPT
from tests.world import World

MID = "M-0002"


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


def real_payment(w, mid=MID):
    return w.conn.execute("SELECT * FROM payments WHERE merchant_id = ? AND status = 'SUCCESS' AND reference IS NOT NULL ORDER BY ts DESC LIMIT 1", (mid,)).fetchone()


def codes(report):
    return {s.code for s in report.signals}


def test_a_real_payment_is_recognised_as_real(world):
    p = real_payment(world)
    r = check_claim(world.conn, world.clock, MID, Claim(p["amount_paise"], p["reference"], p["ts"]))
    assert r.verdict == REAL and not any(s.severity == "hard" for s in r.signals)


def test_a_real_payment_without_a_reference_still_matches_by_amount_and_time(world):
    p = real_payment(world)
    assert check_claim(world.conn, world.clock, MID, Claim(p["amount_paise"], None, p["ts"])).verdict == REAL


def test_a_reference_that_belongs_to_another_merchants_payment_is_a_scam_signal(world):
    other = real_payment(world, "M-0003")
    r = check_claim(world.conn, world.clock, MID, Claim(other["amount_paise"], other["reference"], other["ts"]))
    assert r.verdict == LIKELY_SCAM and "REFERENCE_OTHER_MERCHANT" in codes(r)


def test_a_real_reference_with_a_different_amount_is_a_scam_signal(world):
    p = real_payment(world)
    r = check_claim(world.conn, world.clock, MID, Claim(p["amount_paise"] + 100, p["reference"], p["ts"]))
    assert r.verdict == LIKELY_SCAM and "REFERENCE_OTHER_AMOUNT" in codes(r)


@pytest.mark.parametrize("ref", ["12345", "UPI12", "upi827499443433", "UPI82749944343X", "PAID-OK"])
def test_a_reference_that_is_not_shaped_like_one_is_a_scam_signal(world, ref):
    r = check_claim(world.conn, world.clock, MID, Claim(5000, ref))
    assert r.verdict == LIKELY_SCAM and "REFERENCE_FORMAT" in codes(r)


def test_a_payment_that_was_never_made_cannot_be_told_either_way_and_is_never_called_a_scam(world):
    r = check_claim(world.conn, world.clock, MID, Claim(1234))
    assert r.verdict == CANNOT_TELL and "NO_MATCHING_PAYMENT" in codes(r) and not any(s.severity == "hard" for s in r.signals)


def test_an_amount_far_above_the_usual_ticket_is_flagged_as_unusual_and_nothing_more(world):
    tickets = [r[0] for r in world.conn.execute("SELECT amount_paise FROM payments WHERE merchant_id = ? AND status = 'SUCCESS'", (MID,))]
    r = check_claim(world.conn, world.clock, MID, Claim(int(statistics.median(tickets)) * OUTLIER_TICKET_X * 2))
    assert "AMOUNT_OUTLIER" in codes(r) and r.verdict == CANNOT_TELL


def test_a_day_far_above_a_normal_day_is_flagged_once_the_claim_is_placed_in_the_sandbox(world):
    normal = world.conn.execute("SELECT avg_daily_volume_paise FROM merchants WHERE merchant_id = ?", (MID,)).fetchone()[0]
    r = check_claim(world.conn, world.clock, MID, Claim(normal * DAY_STRETCH_X))
    assert "DAY_FAR_ABOVE_NORMAL" in codes(r)


def test_a_payment_claimed_while_the_shop_was_shut_is_flagged(world):
    r = check_claim(world.conn, world.clock, MID, Claim(1234, None, "2026-10-31T03:00:00+05:30"))
    assert "OUTSIDE_SHOP_HOURS" in codes(r)


def test_a_payment_that_is_only_pending_says_so_and_does_not_count_as_real(world):
    now = world.clock.now()
    world.conn.execute("INSERT INTO payments(payment_id, merchant_id, amount_paise, status, ts, reference) VALUES ('P-T-PEND', ?, 77700, 'PENDING', ?, 'UPI900000000099')",
                       (MID, (now - timedelta(minutes=5)).isoformat()))
    r = check_claim(world.conn, world.clock, MID, Claim(77700))
    assert r.verdict == CANNOT_TELL and "ONLY_PENDING" in codes(r)


def test_the_real_books_are_never_touched(world):
    def snapshot():
        tables = sorted(r[0] for r in world.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        return tables, world.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    before = snapshot()
    p = real_payment(world)
    for claim in (Claim(p["amount_paise"], p["reference"], p["ts"]), Claim(99, "12345"), Claim(1234)):
        r = check_claim(world.conn, world.clock, MID, claim)
        assert r.discarded is True
    assert snapshot() == before and world.conn.execute("SELECT COUNT(*) FROM payments WHERE payment_id = 'CLAIM'").fetchone()[0] == 0


def test_the_report_says_what_was_copied_into_the_sandbox(world):
    n = world.conn.execute("SELECT COUNT(*) FROM payments WHERE merchant_id = ?", (MID,)).fetchone()[0]
    r = check_claim(world.conn, world.clock, MID, Claim(1234))
    assert 0 < r.copied["payments"] <= n and r.copied["references"] >= r.copied["payments"]


def test_the_report_is_plain_data_for_the_dashboard(world):
    d = check_claim(world.conn, world.clock, MID, Claim(1234)).as_dict()
    assert json.loads(json.dumps(d)) == d and {"merchant_id", "verdict", "signals", "copied", "discarded"} <= set(d)


def test_a_claim_of_nothing_or_for_nobody_is_refused(world):
    with pytest.raises(ValueError):
        check_claim(world.conn, world.clock, MID, Claim(0))
    with pytest.raises(ValueError):
        check_claim(world.conn, world.clock, MID, Claim(-5))
    with pytest.raises(KeyError):
        check_claim(world.conn, world.clock, "M-9999", Claim(100))
