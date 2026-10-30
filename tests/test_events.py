import pytest

from core.engine.events import EVENT_TYPES, append_event, count_events


def ev(conn, eid, payload=None, type="PAYMENT_SUCCESS"):
    return append_event(conn, event_id=eid, type=type, payload=payload or {"a": 1}, ts_sim="2026-10-31T09:00:00+05:30")


def test_same_event_id_twice_keeps_one_row(conn):
    first = ev(conn, "E-1")
    second = ev(conn, "E-1")
    assert first.inserted and not second.inserted
    assert count_events(conn) == 1


def test_different_ids_with_equal_payloads_keep_two_rows(conn):
    ev(conn, "E-1", {"x": 1})
    ev(conn, "E-2", {"x": 1})
    assert count_events(conn) == 2


def test_unknown_event_type_is_rejected(conn):
    with pytest.raises(ValueError):
        ev(conn, "E-1", type="MADE_UP")


def test_events_are_append_only(conn):
    ev(conn, "E-1")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE events SET type='PAYMENT_FAILED'")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM events")


def test_all_spec_event_types_present():
    assert len(EVENT_TYPES) == 19
