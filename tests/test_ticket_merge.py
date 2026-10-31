"""#91 merged tickets: merge ONLY on the same system object; same merchant + similar text within 48 h is LINK only."""
from datetime import timedelta

import pytest

from core.engine import tickets as T
from core.engine.clock import iso, parse_iso
from core.engine.tickets import TicketGraph, classify_pair

BASE = "2026-10-30T10:00:00+05:30"


def add(conn, tid, mid, obj, summary, created=BASE, typ="DEVICE", status="OPEN"):
    conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,NULL)", (tid, mid, typ, status, obj, summary, created))
    conn.commit()


def test_two_tickets_about_the_same_device_merge(conn, clock):
    add(conn, "T-A", "M-0010", "D-0010", "Soundbox is silent since morning")
    add(conn, "T-B", "M-0010", "D-0010", "speaker not announcing payments", iso(parse_iso(BASE) + timedelta(hours=5)))
    g = TicketGraph(conn, clock)
    res = g.consider("T-B")
    assert res.merged_into == "T-A" and res.linked_to == ()
    assert g.primary_of("T-B") == "T-A" and g.merged_children("T-A") == ["T-B"]


def test_similar_text_on_different_devices_links_only(conn, clock):
    add(conn, "T-A", "M-0010", "D-0010", "Soundbox is silent since morning today")
    add(conn, "T-B", "M-0010", "D-0099", "Soundbox is silent since morning today", iso(parse_iso(BASE) + timedelta(hours=2)))
    g = TicketGraph(conn, clock)
    res = g.consider("T-B")
    assert res.merged_into is None and res.merged_from == ()
    assert res.linked_to == ("T-A",) and g.links_of("T-A") == ["T-B"]
    assert g.primary_of("T-B") == "T-B"
    assert conn.execute("SELECT COUNT(*) FROM ticket_merges").fetchone()[0] == 0


def test_tickets_without_a_system_object_never_merge(conn, clock):
    add(conn, "T-A", "M-0010", None, "Soundbox is silent since morning today")
    add(conn, "T-B", "M-0010", None, "Soundbox is silent since morning today")
    add(conn, "T-C", "M-0010", "", "Soundbox is silent since morning today")
    assert classify_pair({"system_object": None, "merchant_id": "m", "created_at": BASE, "summary": "x y z"},
                         {"system_object": None, "merchant_id": "m", "created_at": BASE, "summary": "a b c"}) == "none"
    g = TicketGraph(conn, clock)
    for t in ("T-B", "T-C"):
        g.consider(t)
    assert conn.execute("SELECT COUNT(*) FROM ticket_merges").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ticket_links").fetchone()[0] == 3     # all three pairs linked, none merged


def test_link_window_is_48_hours_and_same_merchant(conn, clock):
    mk = lambda mid, h: {"merchant_id": mid, "system_object": None, "summary": "settlement delayed again please help",
                         "created_at": iso(parse_iso(BASE) + timedelta(hours=h))}
    assert classify_pair(mk("M-1", 0), mk("M-1", 47)) == "link"
    assert classify_pair(mk("M-1", 0), mk("M-1", 49)) == "none"
    assert classify_pair(mk("M-1", 0), mk("M-2", 1)) == "none"          # different merchant: nothing, not even a link


def test_dissimilar_text_same_merchant_no_relation():
    a = {"merchant_id": "M-1", "system_object": "D-1", "summary": "battery drains fast", "created_at": BASE}
    b = {"merchant_id": "M-1", "system_object": "S-9", "summary": "settlement missing yesterday", "created_at": BASE}
    assert classify_pair(a, b) == "none"


def test_a32_auto_merge_never_joins_different_system_objects(conn, clock):
    """Assurance A32: identical text, same merchant, minutes apart - different objects stay separate tickets."""
    add(conn, "T-A", "M-0020", "S-0100", "money not received in my account")
    add(conn, "T-B", "M-0020", "S-0101", "money not received in my account", iso(parse_iso(BASE) + timedelta(minutes=5)))
    add(conn, "T-C", "M-0020", "D-0020", "money not received in my account", iso(parse_iso(BASE) + timedelta(minutes=9)))
    g = TicketGraph(conn, clock)
    for t in ("T-B", "T-C"):
        g.consider(t)
    assert conn.execute("SELECT COUNT(*) FROM ticket_merges").fetchone()[0] == 0
    assert {g.primary_of(t) for t in ("T-A", "T-B", "T-C")} == {"T-A", "T-B", "T-C"}


def test_merge_is_idempotent_and_order_independent(conn, clock):
    add(conn, "T-A", "M-0010", "S-1", "a b c")
    add(conn, "T-B", "M-0010", "S-1", "d e f", iso(parse_iso(BASE) + timedelta(hours=1)))
    add(conn, "T-C", "M-0010", "S-1", "g h i", iso(parse_iso(BASE) + timedelta(hours=2)))
    g = TicketGraph(conn, clock)
    g.consider("T-B"); g.consider("T-B"); g.consider("T-C")
    assert g.primary_of("T-B") == "T-A" and g.primary_of("T-C") == "T-A"
    assert g.merged_children("T-A") == ["T-B", "T-C"]


def test_merge_never_writes_the_tickets_table(conn, clock):
    add(conn, "T-A", "M-0010", "D-1", "x")
    add(conn, "T-B", "M-0010", "D-1", "y", iso(parse_iso(BASE) + timedelta(hours=1)))
    before = [tuple(r) for r in conn.execute("SELECT * FROM tickets ORDER BY ticket_id")]
    TicketGraph(conn, clock).consider("T-B")
    assert [tuple(r) for r in conn.execute("SELECT * FROM tickets ORDER BY ticket_id")] == before


def test_parent_cleared_makes_each_child_recheck_its_own_done_check(conn, clock):
    for t in ("T-P", "T-C1", "T-C2", "T-C3"):
        add(conn, t, "M-0010", None, t)
    g = TicketGraph(conn, clock)
    for c in ("T-C1", "T-C2", "T-C3"):
        g.attach_child("T-P", c, incident_id="INC-1")
    asked = []
    healed = {"T-C1": True, "T-C2": False, "T-C3": True}
    result = g.parent_cleared("T-P", lambda cid: asked.append(cid) or healed[cid])
    assert asked == ["T-C1", "T-C2", "T-C3"]
    assert result == healed                       # T-C2 is NOT closed just because the parent cleared
    with pytest.raises(ValueError):
        g.attach_child("T-P", "T-P")


def test_auto_close_needs_proof_and_confirmation(conn, clock):
    add(conn, "T-A", "M-0010", "D-1", "x")
    g = TicketGraph(conn, clock)
    assert g.evaluate_close("T-A", done_check_passed=True, merchant_confirmed=False).code == "AWAITING_CONFIRMATION"
    assert g.evaluate_close("T-A", done_check_passed=False, merchant_confirmed=True).code == "DONE_CHECK_FAILED"
    assert conn.execute("SELECT COUNT(*) FROM ticket_closures").fetchone()[0] == 0
    assert g.evaluate_close("T-A", done_check_passed=True, merchant_confirmed=True).raise_ticket is True
    assert conn.execute("SELECT COUNT(*) FROM ticket_closures").fetchone()[0] == 1


def test_reopen_within_seven_days_only(conn, clock):
    add(conn, "T-A", "M-0010", "D-1", "x")
    g = TicketGraph(conn, clock)
    assert g.evaluate_reopen("T-A", problem_returned=True).code == "NOT_AUTO_CLOSED"
    g.evaluate_close("T-A", done_check_passed=True, merchant_confirmed=True)
    assert g.evaluate_reopen("T-A", problem_returned=False).code == "NO_RECURRENCE"
    clock.advance(timedelta(days=6, hours=23))
    assert g.evaluate_reopen("T-A", problem_returned=True).code == "REOPEN"
    assert conn.execute("SELECT reopened_at FROM ticket_closures").fetchone()[0] is not None
    # a second ticket, closed then returning on day 8, is a new ticket
    add(conn, "T-B", "M-0010", "D-2", "y")
    g.evaluate_close("T-B", done_check_passed=True, merchant_confirmed=True)
    clock.advance(timedelta(days=7, hours=1))
    assert g.evaluate_reopen("T-B", problem_returned=True).code == "WINDOW_PASSED"
