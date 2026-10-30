"""The shared field namespace that every playbook's done-check reads. Seeded merchants make the expected values checkable."""
import pytest

from core.engine.fields import SYSTEM_FIELDS, build_namespace
from packs.paytm_merchant.actions import build_registry
from tests.world import World

ALL_READS = sorted(set(SYSTEM_FIELDS.values()))


@pytest.fixture
def ns(db_path, clock):
    w = World(db_path, clock)
    return lambda mid, facts=None: build_namespace(w.reader, mid, ALL_READS, facts), w


def test_every_field_has_a_registered_r0_read():
    reg = build_registry()
    for field, read in SYSTEM_FIELDS.items():
        assert reg.has(read) and reg.get(read).cls.value == "R0", (field, read)


def test_every_system_field_is_populated_for_a_seeded_merchant(ns):
    build, _ = ns
    got = build("M-0002")
    missing = [f for f in SYSTEM_FIELDS if f not in got]
    assert missing == []


def test_values_for_scripted_merchants(ns):
    build, w = ns
    m2 = build("M-0002")
    assert m2["risk.has_open_hold"] is True and m2["settlement.status"] in ("HELD_RISK", "SETTLED", "PENDING")
    assert m2["ticket.open_count"] >= 1 and m2["ticket.latest_status"] == "OPEN"
    m3 = build("M-0003")
    assert m3["lender.eligible"] is True and m3["lender.max_amount_paise"] > 0 and m3["lender.app_status"] == "NONE"
    assert m3["plan.status"] == "ACTIVE" and m3["plan.change_status"] == "NONE" and m3["refund.status"] == "NONE" and m3["closure.status"] == "NONE"
    assert build("M-0001")["lender.eligible"] is False
    assert build("M-0007")["device.status"] == "NONE" and build("M-0007")["device.order_status"] == "NONE"


def test_fields_track_live_state_after_a_write(ns):
    build, w = ns
    assert build("M-0003")["offer.latest_status"] == "NONE"
    r = w.http.post("/mock/offers/waiver", json={"merchant_id": "M-0003", "months": 1}, headers={"Idempotency-Key": "k1"})
    assert build("M-0003")["offer.latest_status"] == "APPLIED"
    w.http.delete(f"/mock/offers/waiver/{r.json()['waiver_id']}", headers={"Idempotency-Key": "k2"})
    assert build("M-0003")["offer.latest_status"] == "REVOKED"
    w.http.post("/mock/lender/application-link", json={"merchant_id": "M-0003"}, headers={"Idempotency-Key": "k3"})
    assert build("M-0003")["lender.app_status"] == "LINK_SENT"
    w.http.post("/mock/devices/M-0003/order", json={}, headers={"Idempotency-Key": "k4"})
    assert build("M-0003")["device.order_status"] == "PLACED"


def test_s1_still_closes_the_same_way(db_path, clock):
    from tests.test_checkpoint_s1 import test_s1_runs_end_to_end_from_stalled_signup_to_first_settlement as s1
    s1(World(db_path, clock, __import__("tests.test_checkpoint_s1", fromlist=["SCRIPT"]).SCRIPT))
