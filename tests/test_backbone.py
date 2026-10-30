"""#118 E4: event backbone: durable cursors, ordered per merchant, at-least-once with dedupe by event id."""
import pytest

from core.actions.executor import Executor  # noqa: F401  (handlers in real use go through the executor)
from core.engine.backbone import Backbone
from core.engine.events import append_event


def ev(conn, eid, merchant, type_="PAYMENT_SUCCESS", **payload):
    append_event(conn, event_id=eid, type=type_, payload=payload, ts_sim="2026-01-01T00:00:00+05:30", merchant_id=merchant)


class Rec:
    def __init__(self, fail_on=None):
        self.seen, self.fail_on = [], set(fail_on or ())

    def __call__(self, e):
        if e.event_id in self.fail_on:
            self.fail_on.discard(e.event_id)
            raise RuntimeError("boom")
        self.seen.append(e.event_id)


def test_delivers_in_order_per_merchant_and_advances_cursor(conn):
    bb, r = Backbone(conn), Rec()
    for i, m in enumerate(["M-1", "M-2", "M-1", "M-2", "M-1"]):
        ev(conn, f"e{i}", m)
    bb.subscribe("c", r)
    assert bb.poll("c") == 5
    assert r.seen == ["e0", "e1", "e2", "e3", "e4"]
    assert bb.cursor("c") == conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] and bb.lag("c") == 0
    assert bb.poll("c") == 0


def test_cursor_is_durable_across_instances(conn):
    ev(conn, "a", "M-1"); ev(conn, "b", "M-1")
    r1 = Rec(); b1 = Backbone(conn); b1.subscribe("c", r1); b1.poll("c", limit=1)
    ev(conn, "c3", "M-1")
    r2 = Rec(); b2 = Backbone(conn); b2.subscribe("c", r2)      # "restart": new object, same db
    b2.drain("c")
    assert r1.seen == ["a"] and r2.seen == ["b", "c3"]


def test_handler_failure_redelivers_and_keeps_order(conn):
    for i in range(3):
        ev(conn, f"e{i}", "M-1")
    bb, r = Backbone(conn), Rec(fail_on={"e1"})
    bb.subscribe("c", r)
    with pytest.raises(RuntimeError):
        bb.poll("c")
    assert r.seen == ["e0"] and bb.lag("c") == 2         # e1 and e2 still pending, e2 NOT jumped ahead
    bb.poll("c")
    assert r.seen == ["e0", "e1", "e2"]                   # at-least-once: e1 retried


def test_replay_does_not_repeat_side_effects(conn):
    for i in range(4):
        ev(conn, f"e{i}", "M-1")
    bb, r = Backbone(conn), Rec()
    bb.subscribe("c", r)
    bb.poll("c")
    bb.replay("c", 0)
    assert bb.lag("c") == 4
    assert bb.poll("c") == 0                              # all deduped
    assert r.seen == ["e0", "e1", "e2", "e3"] and bb.lag("c") == 0


def test_duplicate_publish_is_one_event_one_delivery(conn):
    bb, r = Backbone(conn), Rec()
    bb.subscribe("c", r)
    a = bb.publish(event_id="x", type="PAYMENT_FAILED", payload={"n": 1}, ts_sim="t", merchant_id="M-1")
    b = bb.publish(event_id="x", type="PAYMENT_FAILED", payload={"n": 1}, ts_sim="t", merchant_id="M-1")
    assert a.inserted and not b.inserted
    bb.poll("c"); bb.poll("c")
    assert r.seen == ["x"]


def test_consumers_are_independent_and_type_filtered(conn):
    ev(conn, "p", "M-1", "PAYMENT_SUCCESS"); ev(conn, "f", "M-1", "PAYMENT_FAILED")
    bb, r1, r2 = Backbone(conn), Rec(), Rec()
    bb.subscribe("all", r1); bb.subscribe("fails", r2, types={"PAYMENT_FAILED"})
    bb.drain("all"); bb.drain("fails")
    assert r1.seen == ["p", "f"] and r2.seen == ["f"] and bb.lag("fails") == 0


def test_replayed_topic_makes_no_duplicate_executor_effects(db_path, clock, http, conn):
    """E4 end to end: a handler that raises a ticket through the real executor; replaying the topic adds no second ticket."""
    from core.actions.types import ActionIntent
    from tests.test_executor import AllowAll, SpyHttp, make_executor
    spy = SpyHttp(http)
    ex = make_executor(db_path, clock, spy)
    bb = Backbone(ex.conn)

    def handler(e):
        ex.submit(ActionIntent("raise_ticket", {"merchant_id": e.merchant_id, "type": "OTHER", "system_object": e.event_id,
                                                "summary": "s"}, case_id="C-1", allowlist=("raise_ticket",)))
    ev(ex.conn, "e1", "M-0001"); ev(ex.conn, "e2", "M-0001")
    bb.subscribe("c", handler)
    bb.drain("c")
    assert len(spy.writes()) == 2
    bb.replay("c", 0)
    bb.drain("c")
    assert len(spy.writes()) == 2
    # even with the dedupe table wiped (worst case), the executor's deterministic keys stop the second effect
    ex.conn.execute("DELETE FROM backbone_delivered"); bb.replay("c", 0); bb.drain("c")
    assert len(spy.writes()) == 2
    ex.conn.close()
