"""Shop opening hours: reported versus assumed, open and closed, the next opening, and refusing bad input."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.api.deps import open_ro
from field.shop_hours import ASSUMED, REPORTED, default_for, get_hours, set_hours, to_minutes

IST = timezone(timedelta(hours=5, minutes=30))
SAT = datetime(2026, 10, 31, 0, 0, tzinfo=IST)        # 31 October 2026 is a Saturday
MON = datetime(2026, 11, 2, 0, 0, tzinfo=IST)


@pytest.fixture
def conn(db_path):
    from core.memory.store import connect
    return connect(db_path)


def at(day: datetime, hhmm: str) -> datetime:
    return day.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))


def test_a_merchant_with_no_reported_hours_gets_a_typical_day_marked_assumed(conn):
    h = get_hours(conn, "M-0001")                      # a kirana
    assert (h.open_min, h.close_min, h.source) == (to_minutes("08:00"), to_minutes("21:00"), ASSUMED)
    assert h.open_minutes == 13 * 60 and h.open_hours == 13


def test_each_kind_of_shop_has_its_own_typical_day():
    assert default_for("restaurant").open_min == to_minutes("11:00") and default_for("bakery").open_min == to_minutes("07:00")
    assert default_for("something unknown").source == ASSUMED and default_for(None).close_min == to_minutes("20:00")


def test_reported_hours_are_saved_and_marked_reported(conn, clock):
    h = set_hours(conn, clock, "M-0001", "09:30", "18:00", ("sun",))
    assert (h.open_min, h.close_min, h.closed_days, h.source) == (to_minutes("09:30"), to_minutes("18:00"), ("sun",), REPORTED)
    assert get_hours(conn, "M-0001") == h and get_hours(conn, "M-0002").source == ASSUMED          # another merchant is untouched


def test_changing_hours_replaces_them_not_adds(conn, clock):
    set_hours(conn, clock, "M-0001", "09:00", "17:00")
    set_hours(conn, clock, "M-0001", "10:00", "19:00")
    assert conn.execute("SELECT COUNT(*) FROM shop_hours WHERE merchant_id = 'M-0001'").fetchone()[0] == 1
    assert get_hours(conn, "M-0001").open_min == to_minutes("10:00")


def test_open_exactly_at_opening_and_closed_exactly_at_closing(conn):
    h = get_hours(conn, "M-0001")
    assert h.is_open(at(SAT, "08:00")) and h.is_open(at(SAT, "20:59"))
    assert not h.is_open(at(SAT, "07:59")) and not h.is_open(at(SAT, "21:00"))


def test_a_closed_weekday_is_closed_all_day(conn, clock):
    h = set_hours(conn, clock, "M-0001", "09:00", "18:00", ("mon",))
    assert not h.is_open(at(MON, "12:00")) and h.is_open(at(SAT, "12:00"))


def test_the_next_opening_after_closing_is_the_next_morning(conn):
    h = get_hours(conn, "M-0001")
    assert h.next_open(at(SAT, "22:00")) == at(SAT, "08:00") + timedelta(days=1)
    assert h.next_open(at(SAT, "12:00")) == at(SAT, "12:00")                                # already open: now
    assert h.next_open(at(SAT, "06:00")) == at(SAT, "08:00")                                # before opening: today's opening


def test_the_next_opening_skips_closed_days(conn, clock):
    h = set_hours(conn, clock, "M-0001", "09:00", "18:00", ("sun",))
    assert h.next_open(at(SAT, "19:00")) == at(MON, "09:00")                                # Sunday is skipped


def test_a_shop_closed_every_day_is_never_open_and_says_so(conn, clock):
    h = set_hours(conn, clock, "M-0001", "09:00", "18:00", ("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    with pytest.raises(ValueError):
        h.next_open(at(SAT, "10:00"))


@pytest.mark.parametrize("o,c,days", [("9am", "18:00", ()), ("09:00", "25:00", ()), ("18:00", "09:00", ()), ("09:00", "09:00", ()), ("09:00", "18:00", ("funday",))])
def test_bad_input_is_refused_and_nothing_is_saved(conn, clock, o, c, days):
    with pytest.raises(ValueError):
        set_hours(conn, clock, "M-0001", o, c, days)
    assert get_hours(conn, "M-0001").source == ASSUMED


def test_an_unknown_merchant_is_refused(conn, clock):
    with pytest.raises(KeyError):
        set_hours(conn, clock, "M-9999", "09:00", "18:00")


def test_reading_hours_works_on_a_read_only_connection_before_any_hours_were_reported(db_path):
    ro = open_ro(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("SELECT * FROM shop_hours")                                         # the table really is not there yet
        assert get_hours(ro, "M-0001").source == ASSUMED
    finally:
        ro.close()


def test_reported_hours_are_ledgered_without_anything_but_the_facts(conn, clock):
    from core.audit.ledger import Ledger
    led = Ledger(conn, clock)
    set_hours(conn, clock, "M-0001", "09:00", "18:00", ("sun",), ledger=led)
    e = [x for x in led.entries() if x.payload.get("shop_hours")][-1]
    assert e.payload == {"shop_hours": "M-0001", "open": "09:00", "close": "18:00", "closed_days": ["sun"], "source": REPORTED}
