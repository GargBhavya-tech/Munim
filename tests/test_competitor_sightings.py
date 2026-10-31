"""A rival QR in a rep's photo: fields only, code decides, and only a confident known rival becomes the fact the dip diagnosis acts on."""
import pytest

from core.memory.profile import ProfileStore
from jobs.competitor_sightings import (CONFIRMED, FACT_KEY, MIN_CONFIDENCE, NEEDS_REVIEW, REJECTED, RIVALS, BadReading, NotAllowed, SightingBook, validate_reading)
from tests.checkpoint_script import SCRIPT
from tests.world import World

MID = "M-0003"


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


@pytest.fixture
def book(world):
    return SightingBook(world.conn, world.clock, world.ledger)


@pytest.fixture
def rep(world):
    return world.conn.execute("SELECT rep_id FROM reps ORDER BY rep_id LIMIT 1").fetchone()[0]


def reading(**kw):
    return {"qr_visible": True, "network": "rival_a", "confidence": 0.9, **kw}


def fact(world, mid=MID):
    f = ProfileStore(world.conn, world.clock).get(mid, FACT_KEY)
    return None if f is None or f.valid_to else f


def test_a_confident_known_rival_is_confirmed_and_becomes_the_fact_the_diagnosis_uses(world, book, rep):
    s = book.record(rep, MID, "photo-1", reading())
    assert s.status == CONFIRMED and s.reason is None
    f = fact(world)
    assert f.value == "rival_a" and f.evidence_ref == "photo-1" and f.source == "HUMAN_AGENT"


def test_the_health_job_now_sees_the_competitor_signal(world, book, rep):
    from jobs.health import HealthJob
    book.record(rep, MID, "photo-1", reading())
    assert HealthJob(world.conn, world.clock)._has_competitor_signal(MID) is True and HealthJob(world.conn, world.clock)._has_competitor_signal("M-0009") is False


@pytest.mark.parametrize("kw,status", [({"qr_visible": False, "network": None}, REJECTED), ({"network": None}, REJECTED), ({"network": "unknown_net"}, NEEDS_REVIEW),
                                       ({"confidence": MIN_CONFIDENCE - 0.01}, NEEDS_REVIEW)])
def test_an_unsure_or_unknown_or_empty_reading_writes_no_fact(world, book, rep, kw, status):
    assert book.record(rep, MID, "photo-2", reading(**kw)).status == status and fact(world) is None


def test_exactly_the_minimum_confidence_is_enough(world, book, rep):
    assert book.record(rep, MID, "photo-3", reading(confidence=MIN_CONFIDENCE)).status == CONFIRMED


def test_a_person_can_accept_an_unsure_known_rival_and_only_then_the_fact_is_written(world, book, rep):
    s = book.record(rep, MID, "photo-4", reading(confidence=0.5))
    assert fact(world) is None
    done = book.review(s.sighting_id, "sales_lead", "asha", True)
    assert done.status == CONFIRMED and done.reviewed_by == "sales_lead:asha" and fact(world).value == "rival_a"


def test_rejecting_a_sighting_writes_nothing_and_it_cannot_be_reviewed_twice(world, book, rep):
    s = book.record(rep, MID, "photo-5", reading(confidence=0.5))
    assert book.review(s.sighting_id, "ops_admin", "ravi", False).status == REJECTED and fact(world) is None
    with pytest.raises(ValueError):
        book.review(s.sighting_id, "ops_admin", "ravi", True)


def test_an_unknown_network_can_never_be_accepted(world, book, rep):
    s = book.record(rep, MID, "photo-6", reading(network="unknown_net"))
    with pytest.raises(ValueError):
        book.review(s.sighting_id, "sales_lead", "asha", True)


def test_only_a_sales_lead_or_an_ops_admin_may_review(world, book, rep):
    s = book.record(rep, MID, "photo-7", reading(confidence=0.5))
    with pytest.raises(NotAllowed):
        book.review(s.sighting_id, "support_executive", "x", True)


@pytest.mark.parametrize("bad", [{"qr_visible": True, "network": "rival_a", "confidence": 0.9, "is_competitor": True}, {"qr_visible": True, "confidence": 0.9, "verdict": "switched"},
                                 {"qr_visible": True}, {"qr_visible": "yes", "confidence": 0.5}, {"qr_visible": True, "confidence": 1.5}, {"qr_visible": True, "confidence": True}, "a rival"])
def test_a_reading_with_a_verdict_or_missing_or_malformed_fields_is_refused(bad):
    with pytest.raises(BadReading):
        validate_reading(bad)


def test_the_network_name_is_normalised_but_must_still_be_on_the_list(world, book, rep):
    assert book.record(rep, MID, "photo-8", reading(network="  RIVAL_B ")).network == "rival_b" and "rival_b" in RIVALS


def test_unknown_people_or_a_missing_photo_are_refused(world, book, rep):
    with pytest.raises(KeyError):
        book.record("R-NOPE", MID, "p", reading())
    with pytest.raises(KeyError):
        book.record(rep, "M-9999", "p", reading())
    with pytest.raises(BadReading):
        book.record(rep, MID, "  ", reading())


def test_the_ledger_records_ids_and_status_only(world, book, rep):
    book.record(rep, MID, "photo-secret-ref", reading())
    e = [x for x in world.ledger.entries() if x.payload.get("competitor_sighting")][-1]
    assert set(e.payload) == {"competitor_sighting", "sighting_id", "status"} and "photo" not in str(e.payload)
