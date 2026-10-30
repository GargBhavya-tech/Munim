"""#86 Scam Shield: 20 synthetic screenshots (fakes -> NO_MATCH, reals -> MATCH, misread amount -> AMBIGUOUS/ask), the vision model may
not judge, and the whole case through engine, executor and mocks: fraud flag + escalation on MISMATCH, one re-check, fraud-call reports."""
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from core.engine.scripted import scripted_decider
from core.vision.read_screenshot import InvalidReading, LLMVisionReader, SyntheticSvgReader, parse_reading
from packs.paytm_merchant import scam_shield as ss
from tests.fakes_gate import llm
from tests.world import World

SHOTS = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "fixtures" / "screenshots"
MANIFEST = yaml.safe_load((SHOTS / "manifest.yaml").read_text(encoding="utf-8"))
MID = MANIFEST["merchant_id"]


def world_with_payments(db_path, clock):
    w = World(db_path, clock, None, mid=MID)
    setups = sorted((s for row in MANIFEST["screenshots"] for s in row["setup"]), key=lambda s: s["minute"])
    start = clock.now()
    for n, s in enumerate(setups):
        target = start + timedelta(minutes=s["minute"])
        if target > clock.now():
            clock.advance(target - clock.now())
        r = w.http.post("/mock/payments/simulate", json={k: s[k] for k in ("merchant_id", "amount_paise", "reference", "status")},
                        headers={"Idempotency-Key": f"shot-pay-{n}"})
        assert r.status_code == 201, r.text
    clock.advance(start + timedelta(minutes=15) - clock.now())
    return w


@pytest.fixture
def w(db_path, clock):
    return world_with_payments(db_path, clock)


def test_there_are_twenty_synthetic_screenshots_covering_every_verdict():
    rows = MANIFEST["screenshots"]
    assert len(rows) == 20 and MANIFEST["provenance"] == "SYNTHETIC"
    assert {r["expected"] for r in rows} == {"MATCH", "NO_MATCH", "AMBIGUOUS", "MISMATCH", "PENDING"}
    assert all("SIMULATED" in (SHOTS / r["file"]).read_text(encoding="utf-8") for r in rows)


@pytest.mark.parametrize("row", MANIFEST["screenshots"], ids=lambda r: r["file"])
def test_each_screenshot_gets_the_expected_verdict_from_code(w, row):
    draft = SyntheticSvgReader().read((SHOTS / row["file"]).read_bytes())
    v = ss.verify(w.reader, MID, draft.as_payload(), w.clock.now())
    assert v.status == row["expected"], (row["file"], v)
    if v.status == "MATCH":
        assert v.payment["reference"] == row["reference"] and v.payment["merchant_id"] == MID


def test_the_vision_model_may_not_judge_and_a_low_confidence_field_is_not_trusted():
    with pytest.raises(InvalidReading):
        parse_reading({"amount_paise": 200000, "verdict": "looks fake"})
    with pytest.raises(InvalidReading):
        parse_reading({"amount_paise": 200000, "is_fake": True})
    d = parse_reading({"amount_paise": 200000, "time_hhmm": "14:05", "confidence": {"amount": 0.5, "time": 0.95}})
    assert d.amount_paise is None and d.time_hhmm == "14:05" and d.low_confidence == ["amount", "reference", "app"]


def test_the_llm_vision_reader_retries_once_then_reads_nothing(w):
    client, prov = llm([{"verdict": "fake"}, "not json"])
    d = LLMVisionReader(client).read(b"\x89PNG fake", "image/png")
    assert d.amount_paise is None and d.source == "vision_invalid" and len(prov.seen) == 2
    assert prov.seen[0][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_another_merchants_payment_is_never_disclosed(w):
    other = next(r for r in MANIFEST["screenshots"] if r["kind"] == "reused")
    body = w.reader("get_payment_by_reference", {"merchant_id": MID, "reference": other["reference"]})
    assert body == {"merchant_id": MID, "reference": other["reference"], "exists": True, "belongs_to_merchant": False, "payment": None}


# ---------------------------------------------------------------------------------------------------- the case, end to end
def scripted(w):
    h = ss.make_handler(w.conn, w.reader, w.clock, w.ledger)
    w.engine.decider = scripted_decider({(ss.PLAYBOOK_ID, "answer"): lambda ctx: h(ctx, "en-IN")})
    return w


def show(w, eid, file):
    draft = SyntheticSvgReader().read((SHOTS / file).read_bytes())
    w.inject(eid, "MERCHANT_MESSAGE", payload={"text": "[payment screenshot]", "intent": "VERIFY_PAYMENT", "screenshot": draft.as_payload()})
    w.dispatcher.run()


def sent(w):
    return [json.loads(r[0])["text"] for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='MESSAGE_SENT' ORDER BY seq")]


def test_a_real_screen_is_confirmed_with_the_systems_amount_and_the_case_closes(w):
    scripted(w)
    show(w, "S-1", "real_02.svg")
    case = w.case(ss.PLAYBOOK_ID)
    assert case.state == "RESOLVED" and case.lane == "P0"
    assert sent(w) == ["Payment of ₹167 received at 09:03. You can hand over the goods."]


def test_a_fake_screen_says_do_not_hand_over_and_is_rechecked_once(w):
    scripted(w)
    show(w, "S-1", "fake_01.svg")
    case = w.case(ss.PLAYBOOK_ID)
    assert case.state == "WAITING" and "Do not hand over the goods" in sent(w)[-1]
    w.clock.advance(timedelta(minutes=16))                          # the re-check fires; still nothing: the case closes quietly
    assert w.engine.repo.load(case.case_id).state == "RESOLVED" and len(sent(w)) == 1


def test_a_payment_that_lands_after_no_match_is_reported_on_the_recheck(w):
    scripted(w)
    w.inject("S-1", "MERCHANT_MESSAGE", payload={"text": "did 450 come?", "intent": "VERIFY_PAYMENT"})
    w.dispatcher.run()
    assert "Do not hand over" in sent(w)[-1]
    w.clock.advance(timedelta(minutes=2))
    w.pay("late-pay", 45000); w.dispatcher.run()
    w.clock.advance(timedelta(minutes=15)); w.dispatcher.run()
    assert sent(w)[-1].startswith("Update: the payment of ₹450 has now arrived, at ")
    assert w.case(ss.PLAYBOOK_ID).state == "RESOLVED"


def test_a_mismatch_flags_fraud_once_and_hands_to_a_person(w):
    scripted(w)
    show(w, "S-1", "edited_amount.svg")
    case = w.case(ss.PLAYBOOK_ID)
    assert case.state == "ESCALATED" and sent(w)[-1].startswith("Warning:")
    flags = w.reader("get_risk_status", {"merchant_id": MID})["flags"]
    assert [f["kind"] for f in flags if f["status"] == "OPEN"] == ["REFERENCE_MISMATCH"]
    reasons = json.loads(w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ESCALATION'").fetchone()[0])["reasons"]
    assert reasons[0].startswith("FRAUD_SIGNAL")


def test_a_fraud_call_report_warns_and_flags(w):
    scripted(w)
    w.inject("S-1", "MERCHANT_MESSAGE", payload={"text": "someone called asking for my OTP", "intent": "REPORT_FRAUD_CALL"})
    w.dispatcher.run()
    assert sent(w)[-1] == "Paytm never asks for your PIN or OTP. Do not share them with anyone. I have reported this call to our risk team."
    assert [f["kind"] for f in w.reader("get_risk_status", {"merchant_id": MID})["flags"]][-1] == "FRAUD_CALL"


def test_a_merchant_with_no_open_case_is_routed_to_scam_shield_by_intent(w):
    scripted(w)
    w.inject("S-1", "MERCHANT_MESSAGE", payload={"text": "payment aaya kya 167"})
    w.dispatcher.run()
    assert w.case(ss.PLAYBOOK_ID).state == "RESOLVED"


def test_the_screenshot_endpoint_reads_decides_and_never_stores_the_image(w):
    from core.api.app import create_api
    scripted(w)
    api = TestClient(create_api(w.db_path, w.clock, w.executor, w.http, registry=w.registry, after_write=w.dispatcher.run))
    img = (SHOTS / "real_01.svg").read_bytes()
    r = api.post(f"/api/merchants/{MID}/screenshot", content=img, headers={"Content-Type": "image/svg+xml"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reading"]["reference"] == "501122334400" and body["replies"][0]["text"].startswith("Payment of ₹1,200 received")
    stored = w.conn.execute("SELECT payload_json FROM events WHERE type='MERCHANT_MESSAGE'").fetchone()[0]
    assert "<svg" not in stored and "SIMULATED" not in stored
    assert api.post(f"/api/merchants/{MID}/screenshot", content=b"\x89PNG", headers={"Content-Type": "image/png"}).status_code == 503


@pytest.mark.parametrize("minutes_later,delivered", [(10, True), (120, False)])
def test_a_code_marked_reply_is_only_a_reply_within_an_hour_of_the_merchants_message(db_path, clock, minutes_later, delivered):
    """M-0003 has a stated contact window that 09:xx falls outside: outreach is refused, a reply is not. The flag cannot stretch."""
    from core.engine.workflow import Decision
    w = World(db_path, clock, None, mid=MID)
    case = w.engine.open_case(MID, "C9_COMPLAINT")
    calls = []

    def decide(ctx):
        calls.append(ctx.event.type)
        return Decision() if ctx.event.type == "MERCHANT_MESSAGE" else Decision(message_en="Your ticket is open.", answers_merchant=True)
    w.engine.decider = decide
    w.inject("Q-1", "MERCHANT_MESSAGE", payload={"text": "kya hua"}); w.dispatcher.run()
    w.clock.advance(timedelta(minutes=minutes_later))
    w.pay("Q-pay", 1000); w.dispatcher.run()
    assert "PAYMENT_SUCCESS" in calls
    assert (len(sent(w)) == 1) is delivered
    refused = [json.loads(r[0]) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind='POLICY'")]
    assert any(p.get("rule_ids") == ["CONTACT-BUDGET"] for p in refused) is (not delivered)
