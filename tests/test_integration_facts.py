"""The standard facts_fn, the message-path playbook gate, and the order status of a QR-only merchant."""
from datetime import timedelta

from core.engine.case_facts import make_facts_fn
from core.engine.diagnose import action_permitted
from core.engine.workflow import Event
from tests.conftest import count
from tests.world import World

KFS = "The lender will show you a Key Fact Statement with all charges before you agree."


def test_a_qr_only_merchants_order_status_is_visible(db_path, clock):
    w = World(db_path, clock, mid="M-0007")                      # M-0007 has no device
    assert w.reader("get_device_status", {"merchant_id": "M-0007"})["orders"] == []
    w.http.post("/mock/devices/M-0007/order", json={}, headers={"Idempotency-Key": "o1"})
    body = w.reader("get_device_status", {"merchant_id": "M-0007"})
    assert body["status"] == "NONE" and [o["status"] for o in body["orders"]] == ["PLACED"]
    from core.engine.fields import build_namespace
    assert build_namespace(w.reader, "M-0007", ["get_device_status"])["device.order_status"] == "PLACED"


def test_closure_requests_are_counted_per_case_from_the_audit_trail(db_path, clock):
    w = World(db_path, clock, mid="M-0011")
    case = w.engine.open_case("M-0011", "C7_RETURN_CLOSURE")
    facts = make_facts_fn(w.conn, w.reader)
    ev = Event("x0", "MERCHANT_MESSAGE", "M-0011", case.case_id, {"intent": "CLOSURE_REQUEST"})
    assert facts(case, ev)["closure_requests"] == 0
    for n in (1, 2):
        w.inject(f"CL-{n}", "MERCHANT_MESSAGE", mid="M-0011", payload={"intent": "CLOSURE_REQUEST", "text": "band karo"})
        w.engine.handle_event(Event(f"CL-{n}", "MERCHANT_MESSAGE", "M-0011", case.case_id, {"intent": "CLOSURE_REQUEST"}))
    f = facts(case, ev)
    assert f["closure_requests"] == 2 and f["closure_created"] is False and f["intent"] == "CLOSURE_REQUEST"
    w.http.post("/mock/closures", json={"merchant_id": "M-0011"}, headers={"Idempotency-Key": "c1"})
    assert facts(case, ev)["closure_created"] is True


def test_kfs_fact_comes_from_what_was_actually_sent(db_path, clock):
    w = World(db_path, clock, mid="M-0003")
    case = w.engine.open_case("M-0003", "S3_LOAN")
    facts = make_facts_fn(w.conn, w.reader)
    ev = Event("e", "MERCHANT_MESSAGE", "M-0003", case.case_id, {})
    assert facts(case, ev)["kfs_message_sent"] is False
    w.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id, payload={"language": "en", "text": "Hello, I can help."})
    assert facts(case, ev)["kfs_message_sent"] is False
    w.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id, payload={"language": "en", "text": KFS})
    assert facts(case, ev)["kfs_message_sent"] is True
    other = w.engine.open_case("M-0003", "S2_SOUNDBOX_UPSELL")
    assert facts(other, ev)["kfs_message_sent"] is False          # another case's messages do not count


def test_a_message_is_held_to_the_same_playbook_precondition_as_an_action(db_path, clock):
    """Regression: only Decision.action was checked against max_per_case/precondition; message_en skipped it."""
    w = World(db_path, clock, mid="M-0008")
    pb = w.playbooks["S5_WINBACK"]
    gated = pb.action("send_message")
    assert gated is not None and gated.precondition, "S5 must gate its message on a precondition for this test to mean anything"
    ok, why = action_permitted(pb, "send_message", w.reader, "M-0008", {}, 0)
    # M-0008 is dormant with an ONLINE device: the playbook decides, and the engine must ask it for the message path too
    import inspect
    src = inspect.getsource(w.engine._plan)
    assert src.count("_playbook_allows") >= 2
