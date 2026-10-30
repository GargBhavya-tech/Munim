"""#92 ordered tickets: lane, then earliest deadline first (hard), value only breaks ties, ageing, reasons, logged overrides."""
import random
from datetime import timedelta

import pytest

from jobs.ticket_order import OrderedQueue, QueueItem, order_queue

RS = 100  # paise per rupee
NOW = None


def build(clock):
    now = clock.now()
    H = lambda h: now + timedelta(hours=h)
    q = lambda tid, lane, dl=None, created=-1, **kw: QueueItem(tid, lane, created_at=H(created), deadline=None if dl is None else H(dl), **kw)
    return [
        # fraud / safety
        q("F1", "FRAUD_SAFETY", 1),
        q("F2", "FRAUD_SAFETY", 1, money_at_risk_paise=50_000 * RS),
        q("F3", "FRAUD_SAFETY", 6, money_at_risk_paise=500_000 * RS),            # large, far from its deadline
        q("F4", "FRAUD_SAFETY", -2, money_at_risk_paise=100 * RS),                # tiny but BREACHED
        # money stuck
        q("M1", "MONEY_STUCK", 3, money_at_risk_paise=12_400 * RS, contacts=2),
        q("M2", "MONEY_STUCK", 30, money_at_risk_paise=900_000 * RS),             # large, far
        q("M3", "MONEY_STUCK", -5, money_at_risk_paise=500 * RS),                 # small, breached
        q("M4", "MONEY_STUCK", None, created=-10),                                # no deadline: implied SLA 24 h => +14 h
        q("M5", "MONEY_STUCK", 3, money_at_risk_paise=2_000 * RS),
        # service down
        q("S1", "SERVICE_DOWN", 2, daily_volume_paise=6_000 * RS, down_since=H(-12)),
        q("S2", "SERVICE_DOWN", 8, daily_volume_paise=6_462_50, down_since=H(-20)),
        q("S3", "SERVICE_DOWN", None, created=-23),                               # ageing: implied SLA +1 h
        q("S4", "SERVICE_DOWN", 8, merchants_affected=30),
        q("S5", "SERVICE_DOWN", 20),
        q("S6", "SERVICE_DOWN", 0.5),
        # information
        q("I1", "INFORMATION", 4),
        q("I2", "INFORMATION", None, created=-70),                                # implied SLA 72 h => +2 h
        q("I3", "INFORMATION", None, created=-1),                                 # +71 h
        q("I4", "INFORMATION", 100, money_at_risk_paise=10_000_000 * RS),         # a crore, far away
        q("I5", "INFORMATION", 4, frustration=5),
    ]


# Written by hand from the rules (lane, then deadline; value only for exact deadline ties):
#  fraud:   F4 (-2 h, breached) < F2 (+1 h, Rs 50,000) < F1 (+1 h, no value) < F3 (+6 h, Rs 5 lakh)
#  money:   M3 (-5 h, breached) < M1 (+3 h, Rs 12,400, 2nd contact) < M5 (+3 h, Rs 2,000) < M4 (+14 h implied) < M2 (+30 h, Rs 9 lakh)
#  service: S6 (+30 min) < S3 (+1 h implied) < S1 (+2 h) < S4 (+8 h, 30 merchants) < S2 (+8 h, sales lost only) < S5 (+20 h)
#  info:    I2 (+2 h implied) < I5 (+4 h, frustration 5) < I1 (+4 h) < I3 (+71 h implied) < I4 (+100 h, Rs 1 crore)
EXPECTED = ["F4", "F2", "F1", "F3", "M3", "M1", "M5", "M4", "M2", "S6", "S3", "S1", "S4", "S2", "S5", "I2", "I5", "I1", "I3", "I4"]


def test_twenty_ticket_fixture_matches_the_hand_written_order(clock):
    items = build(clock)
    assert len(items) == 20
    assert [r.ticket_id for r in order_queue(items, clock.now())] == EXPECTED


def test_order_does_not_depend_on_input_order(clock):
    items = build(clock)
    rnd = random.Random(7)
    for _ in range(5):
        rnd.shuffle(items)
        assert [r.ticket_id for r in order_queue(items, clock.now())] == EXPECTED


def test_a31_small_ticket_near_deadline_outranks_large_ticket_far_away(clock):
    now = clock.now()
    small = QueueItem("small", "MONEY_STUCK", now, deadline=now + timedelta(hours=1), money_at_risk_paise=300 * RS)
    big = QueueItem("big", "MONEY_STUCK", now, deadline=now + timedelta(hours=40), money_at_risk_paise=50_000_000 * RS, merchants_affected=500, frustration=5)
    assert [r.ticket_id for r in order_queue([big, small], now)] == ["small", "big"]


def test_large_ticket_never_jumps_a_breached_deadline(clock):
    now = clock.now()
    breached = QueueItem("breached", "SERVICE_DOWN", now - timedelta(hours=30), deadline=now - timedelta(minutes=1))
    huge = QueueItem("huge", "SERVICE_DOWN", now, deadline=now + timedelta(minutes=1), money_at_risk_paise=10**12,
                     daily_volume_paise=10**9, down_since=now - timedelta(days=9), merchants_affected=999, frustration=5, contacts=9)
    assert [r.ticket_id for r in order_queue([huge, breached], now)] == ["breached", "huge"]


def test_lane_beats_deadline(clock):
    now = clock.now()
    info_breached = QueueItem("info", "INFORMATION", now, deadline=now - timedelta(hours=9))
    fraud_later = QueueItem("fraud", "FRAUD_SAFETY", now, deadline=now + timedelta(hours=9))
    assert [r.ticket_id for r in order_queue([info_breached, fraud_later], now)] == ["fraud", "info"]


def test_ageing_raises_priority_of_a_deadline_less_ticket(clock):
    now = clock.now()
    old = QueueItem("old", "INFORMATION", now - timedelta(hours=60))
    new = QueueItem("new", "INFORMATION", now - timedelta(hours=1))
    assert [r.ticket_id for r in order_queue([new, old], now)] == ["old", "new"]
    # same two tickets later: the newer one has aged past an even later deadline
    later = now + timedelta(hours=30)
    even_newer = QueueItem("newer", "INFORMATION", later - timedelta(minutes=5))
    assert [r.ticket_id for r in order_queue([even_newer, new], later)] == ["new", "newer"]


def test_every_ticket_shows_its_reason(clock):
    ranked = {r.ticket_id: r for r in order_queue(build(clock), clock.now())}
    assert all(r.reason for r in ranked.values())
    assert ranked["M1"].reason == "deadline in 3 h · ₹12,400 · 2nd contact"
    assert ranked["F4"].reason == "deadline breached 2 h ago · ₹100"
    assert ranked["S6"].reason == "deadline in 30 min"
    assert ranked["M4"].reason == "SLA in 14 h"
    assert ranked["F3"].reason == "deadline in 6 h · ₹5,00,000"            # Indian digit grouping
    assert ranked["S4"].reason == "deadline in 8 h · 30 merchants"
    assert "sales lost" in ranked["S1"].reason


def test_overrides_are_applied_and_logged(conn, clock):
    items = build(clock)
    oq = OrderedQueue(conn, clock)
    with pytest.raises(ValueError):
        oq.override(items, "I4", 1, by="  ", reason="x")
    with pytest.raises(ValueError):
        oq.override(items, "I4", 1, by="asha", reason="")
    assert oq.override_log() == []
    oq.override(items, "I4", 1, by="asha", reason="merchant is the CEO's relative")
    ranked = oq.order(items)
    assert [r.ticket_id for r in ranked][:3] == ["I4", "F4", "F2"]
    assert ranked[0].overridden_by == "asha" and "moved by asha" in ranked[0].reason
    assert [r.position for r in ranked] == list(range(1, 21))
    log = oq.override_log()
    assert len(log) == 1 and (log[0]["ticket_id"], log[0]["by_user"], log[0]["from_position"], log[0]["position"]) == ("I4", "asha", 20, 1)
    # a ticket that has left the queue no longer pins anything
    assert [r.ticket_id for r in oq.order([i for i in items if i.ticket_id != "I4"])] == [t for t in EXPECTED if t != "I4"]
