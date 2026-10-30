import json

import pytest

from core.audit.ledger import Ledger
from core.memory.episodes import EpisodeStore, ledger_fallback, verify_sentences
from tests.test_timeline import mk_case, mk_event

A = "M-0001"


@pytest.fixture
def w(conn, clock):
    mk_case(conn, "C-1", A, "2026-10-05T09:00:00+05:30")
    mk_case(conn, "C-2", "M-0002", "2026-10-05T09:00:00+05:30")
    mk_event(conn, "EV-1", A, "C-1", "DEVICE_HEARTBEAT_LOST", "2026-10-05T09:01:00+05:30", {"device": "SB-1"})
    mk_event(conn, "EV-OTHER", "M-0002", "C-2", "DEVICE_DELIVERED", "2026-10-05T09:02:00+05:30")
    led = Ledger(conn, clock)
    led.append(kind="ACTION_RESULT", actor="ENGINE", case_id="C-1", payload={"action": "reset_device", "status": "OK"})
    led.append(kind="ESCALATION", actor="SYSTEM", case_id="C-1", payload={"reasons": ["REPEAT_FAILURE"]})
    return conn


def S(text, cites, refs):
    return {"text": text, "cites": cites, "refs": refs}


def test_false_replacement_delivered_sentence_is_dropped(w):
    good = S("The soundbox lost its heartbeat.", ["EV-1"], ["DEVICE_HEARTBEAT_LOST"])
    nonexistent = S("Replacement was delivered.", ["EV-999"], ["DELIVERED"])
    wrong_content = S("Replacement was delivered.", ["EV-1"], ["DELIVERED"])  # event exists, says nothing about delivery
    other_case = S("Replacement was delivered.", ["EV-OTHER"], ["DEVICE_DELIVERED"])  # real event, but another case
    no_cites = S("Everything was fixed.", [], ["fixed"])
    no_refs = S("Looks fine.", ["EV-1"], [])
    kept, dropped = verify_sentences(w, "C-1", [good, nonexistent, wrong_content, other_case, no_cites, no_refs])
    assert kept == [good] and len(dropped) == 5


def test_every_ref_must_appear_not_just_one(w):
    s = S("Device reset succeeded and was delivered.", ["EV-1", "ledger:1"], ["reset_device", "DELIVERED"])
    assert verify_sentences(w, "C-1", [s])[0] == []


def test_ledger_cite_must_belong_to_the_case(w, clock):
    Ledger(w, clock).append(kind="ACTION_RESULT", actor="ENGINE", case_id="C-2", payload={"action": "reset_device", "status": "OK"})
    s = S("Reset ok.", ["ledger:3"], ["reset_device"])
    assert verify_sentences(w, "C-1", [s])[0] == [] and len(verify_sentences(w, "C-2", [s])[0]) == 1


def test_stored_summary_contains_only_verified_sentences(w, clock):
    def liar(case_id, events):
        return [S("The soundbox lost its heartbeat.", ["EV-1"], ["DEVICE_HEARTBEAT_LOST"]),
                S("Replacement was delivered.", [], ["DELIVERED"]),
                S("Refund of Rs 500 was issued.", ["EV-1"], ["refund"])]
    out = EpisodeStore(w, clock, liar).summarise_case("C-1")
    assert out["dropped"] == 2 and [s["text"] for s in out["sentences"]] == ["The soundbox lost its heartbeat."]
    stored = w.execute("SELECT summary_json FROM episodes WHERE case_id='C-1'").fetchone()[0]
    assert "Replacement" not in stored and "Refund" not in stored and "heartbeat" in stored
    assert w.execute("SELECT merchant_id FROM episodes WHERE case_id='C-1'").fetchone()[0] == A


def test_ledger_fallback_is_fully_verifiable(w, clock):
    raw = ledger_fallback(w, "C-1")
    assert [s["text"] for s in raw] == ["Action reset_device finished with status OK.", "Case was escalated to a human (REPEAT_FAILURE)."]
    out = EpisodeStore(w, clock).summarise_case("C-1")
    assert out["dropped"] == 0 and len(out["sentences"]) == 2
    again = EpisodeStore(w, clock).summarise_case("C-1")  # idempotent per case
    assert w.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1 and again["sentences"] == out["sentences"]


def test_summariser_crash_is_not_swallowed_into_unverified_text(w, clock):
    def boom(c, e):
        raise RuntimeError("model down")
    with pytest.raises(RuntimeError):
        EpisodeStore(w, clock, boom).summarise_case("C-1")
    assert w.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_no_summary_when_memory_withdrawn(w, clock):
    w.execute("INSERT OR REPLACE INTO consent(subject_id,channel,status,source,ts) VALUES (?,?,?,?,?)", (A, "memory", "REVOKED", "t", clock.now_iso()))
    assert EpisodeStore(w, clock).summarise_case("C-1")["stored"] is False
    assert w.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
