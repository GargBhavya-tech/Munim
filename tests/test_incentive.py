"""A rep's incentive follows merchant activity: nothing until the first settlement lands, then in step with what the merchant actually processes."""
import json
from datetime import timedelta

import pytest

from field.incentive import ACTIVATED, ACTIVATION_SHARE_PCT, ACTIVITY_MONTHS, MONTHLY_TARGET_PAISE, NOT_COUNTED, POOL_PAISE, statement
from field.visits import CONFIRMED, SCHEMA_SQL
from tests.checkpoint_script import SCRIPT
from tests.world import World

LEAD, MERCHANT = "L-0001", "M-0009"
ACTIVATION = POOL_PAISE * ACTIVATION_SHARE_PCT // 100
MONTH_SHARE = (POOL_PAISE - ACTIVATION) // ACTIVITY_MONTHS


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    for s in SCHEMA_SQL:
        w.conn.execute(s)
    w.conn.execute("DELETE FROM settlements WHERE merchant_id = ?", (MERCHANT,))          # the seed already settled this merchant: start from a clean slate
    w.conn.execute("DELETE FROM payments WHERE merchant_id = ?", (MERCHANT,))
    return w


@pytest.fixture
def rep(world):
    return world.conn.execute("SELECT rep_id FROM reps ORDER BY rep_id LIMIT 1").fetchone()[0]


def visit(w, rep, *, lead=LEAD, outcome="CONVERTED", status=CONFIRMED, geo=1, photo="p-1", vid="V-1"):
    w.conn.execute("INSERT INTO field_visits VALUES (?,?,?,?,?,?,?,?,?,?,NULL)", (vid, rep, lead, w.clock.now_iso(), json.dumps({"outcome": outcome}), "t", geo, photo, status, "[]"))


def win(w, lead=LEAD, merchant=MERCHANT):
    w.conn.execute("UPDATE leads SET matched_merchant_id = ? WHERE lead_id = ?", (merchant, lead))


def settle(w, at="2026-10-31T09:00:00+05:30", merchant=MERCHANT):
    w.conn.execute("INSERT INTO settlements(settlement_id, merchant_id, amount_paise, status, expected_at, settled_at, reason) VALUES ('S-T1', ?, 100000, 'SETTLED', ?, ?, NULL)", (merchant, at, at))


def pay(w, ts, amount, n, merchant=MERCHANT):
    w.conn.execute("INSERT INTO payments(payment_id, merchant_id, amount_paise, status, ts, reference) VALUES (?,?,?,'SUCCESS',?,?)", (f"P-T{n}", merchant, amount, ts, f"UPI{900000000000 + n}"))


def line(w, rep):
    (l,) = statement(w.conn, w.clock, rep).lines
    return l


def test_the_pool_is_split_into_an_activation_share_and_equal_monthly_shares():
    assert ACTIVATION + MONTH_SHARE * ACTIVITY_MONTHS <= POOL_PAISE and MONTH_SHARE > 0


def test_a_converted_visit_for_a_lead_not_yet_won_counts_for_nothing(world, rep):
    visit(world, rep)
    l = line(world, rep)
    assert (l.status, l.earned_paise, l.pending_paise, l.merchant_id) == (NOT_COUNTED, 0, POOL_PAISE, None) and "not been won" in l.note


def test_a_signup_with_no_settlement_is_not_a_win(world, rep):
    visit(world, rep)
    win(world)
    l = line(world, rep)
    assert (l.status, l.earned_paise, l.pending_paise) == (NOT_COUNTED, 0, POOL_PAISE) and "no settlement" in l.note


def test_the_first_settlement_earns_the_activation_share_and_the_months_are_not_yet(world, rep):
    visit(world, rep)
    win(world)
    settle(world)
    l = line(world, rep)
    assert l.status == ACTIVATED and l.earned_paise == ACTIVATION and l.pending_paise == POOL_PAISE - ACTIVATION and l.first_settlement_at == "2026-10-31T09:00:00+05:30"
    assert [m["status"] for m in l.months] == ["NOT_YET"] * ACTIVITY_MONTHS


def test_a_completed_month_earns_in_step_with_what_the_merchant_processed(world, rep):
    visit(world, rep)
    win(world)
    settle(world)
    pay(world, "2026-11-05T12:00:00+05:30", MONTHLY_TARGET_PAISE // 2, 1)
    world.clock.advance(timedelta(days=31))
    l = line(world, rep)
    m1 = l.months[0]
    assert m1["status"] == "COMPLETE" and m1["volume_paise"] == MONTHLY_TARGET_PAISE // 2 and m1["earned_paise"] == MONTH_SHARE * (MONTHLY_TARGET_PAISE // 2) // MONTHLY_TARGET_PAISE
    assert l.earned_paise == ACTIVATION + m1["earned_paise"] and l.earned_paise + l.pending_paise == POOL_PAISE


def test_a_merchant_who_stops_transacting_stops_earning_for_the_rep(world, rep):
    visit(world, rep)
    win(world)
    settle(world)
    world.clock.advance(timedelta(days=31))
    l = line(world, rep)
    assert l.months[0]["earned_paise"] == 0 and l.earned_paise == ACTIVATION


def test_a_month_is_capped_at_its_full_share_however_much_the_merchant_processes(world, rep):
    visit(world, rep)
    win(world)
    settle(world)
    pay(world, "2026-11-05T12:00:00+05:30", MONTHLY_TARGET_PAISE * 10, 1)
    world.clock.advance(timedelta(days=31))
    assert line(world, rep).months[0]["earned_paise"] == MONTH_SHARE


def test_everything_is_earned_after_the_months_if_the_merchant_hits_the_target_every_month(world, rep):
    visit(world, rep)
    win(world)
    settle(world)
    from datetime import datetime, timezone
    start = datetime(2026, 10, 31, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    for k in range(ACTIVITY_MONTHS):
        pay(world, (start + timedelta(days=30 * k + 5)).isoformat(), MONTHLY_TARGET_PAISE, k)
    world.clock.advance(timedelta(days=30 * ACTIVITY_MONTHS + 2))
    l = line(world, rep)
    assert l.earned_paise == ACTIVATION + MONTH_SHARE * ACTIVITY_MONTHS and all(m["status"] == "COMPLETE" for m in l.months)


@pytest.mark.parametrize("kw", [{"status": "LOGGED"}, {"status": "FLAGGED_UNCONFIRMED"}, {"geo": 0}, {"photo": None}, {"outcome": "NOT_INTERESTED"}, {"outcome": "NEEDS_FOLLOW_UP"}])
def test_only_a_verified_converted_visit_is_attributed(world, rep, kw):
    visit(world, rep, **kw)
    assert statement(world.conn, world.clock, rep).lines == []


def test_a_lead_visited_twice_is_counted_once(world, rep):
    visit(world, rep, vid="V-1")
    visit(world, rep, vid="V-2")
    assert len(statement(world.conn, world.clock, rep).lines) == 1


def test_a_rep_is_credited_only_for_their_own_visits(world, rep):
    other = world.conn.execute("SELECT rep_id FROM reps WHERE rep_id != ? LIMIT 1", (rep,)).fetchone()[0]
    visit(world, other)
    assert statement(world.conn, world.clock, rep).lines == [] and len(statement(world.conn, world.clock, other).lines) == 1


def test_the_totals_add_up_and_the_assumptions_travel_with_the_statement(world, rep):
    visit(world, rep)
    win(world)
    settle(world)
    s = statement(world.conn, world.clock, rep)
    assert s.earned_paise + s.pending_paise == POOL_PAISE and s.assumptions["pool_paise"] == POOL_PAISE and s.as_dict()["lines"][0]["merchant_id"] == MERCHANT


def test_an_unknown_rep_is_refused_and_a_rep_with_no_visits_has_an_empty_statement(world, rep):
    with pytest.raises(KeyError):
        statement(world.conn, world.clock, "R-NOPE")
    assert statement(world.conn, world.clock, rep).lines == [] and statement(world.conn, world.clock, rep).earned_paise == 0
