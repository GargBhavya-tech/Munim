"""One positive and one negative fixture per rule (16 x 2), plus a test for each 'watch out'."""
import copy

import pytest
from pydantic import BaseModel

from core.actions.registry import ActionRegistry, ActionSpec, RiskClass
from core.actions.types import ActionIntent, Principal
from core.engine.policy import load_limits
from packs.paytm_merchant.actions import SPECS, build_registry
from packs.paytm_merchant.policy.rules import LIMITS_PATH, build_engine, normalise_digits


class FakeBudget:
    """Stands in for core.engine.contact_budget (engineer C). check() returns (ok, reason)."""

    def __init__(self, ok=True, why=None):
        self.ok, self.why, self.calls = ok, why, []

    def check(self, subject_id, channel, *, proactive, case_id=None):
        self.calls.append((subject_id, channel, proactive, case_id))
        return (self.ok, None if self.ok else self.why)

    def record(self, subject_id, channel, *, case_id, direction="OUT"):
        pass


def make_reader(http, registry):
    def read(name, args):
        spec = registry.get(name)
        r = http.get(spec.path.format(**args))
        assert r.status_code == 200, r.text
        return r.json()
    return read


def agent(action, args, **kw):
    kw.setdefault("allowlist", (action,))
    return ActionIntent(action, args, principal=Principal("AGENT", "munim"), **kw)


def human(role, action, args, **kw):
    return ActionIntent(action, args, principal=Principal("HUMAN", f"u-{role}", role), **kw)


class MandatePay(BaseModel):
    merchant_id: str
    amount_paise: int


@pytest.fixture
def registry():
    return ActionRegistry([*SPECS, ActionSpec("collect_mandate_payment", RiskClass.R3, MandatePay, description="test-only mandate action")])


@pytest.fixture
def engine(conn, clock, registry, http):
    return build_engine(conn, clock, registry, reader=make_reader(http, registry), contact_budget=FakeBudget())


def ids(d):
    return {v.rule_id for v in d.violations}


WA = {"merchant_id": "M-0001", "channel": "whatsapp"}
MAND = {"mandate_limit_paise": 500_000, "mandate_month_total_paise": 0, "mandate_purpose": "paytm_dues"}

# rule id, fixture name, intent, fires?, expected severity when fired
CASES = [
    ("FIX-FIRST-001", "pos", lambda: agent("create_device_order", {"merchant_id": "M-0001"}, open_case_lanes=("P1",)), True, "BLOCK"),
    ("FIX-FIRST-001", "neg", lambda: agent("create_device_order", {"merchant_id": "M-0001"}, open_case_lanes=("P3",)), False, None),
    ("LOAN-RATE-001", "pos", lambda: agent("send_message", {**WA, "body": "Your loan is ready at 12% interest"}, channel="whatsapp"), True, "BLOCK"),
    ("LOAN-RATE-001", "neg", lambda: agent("send_message", {**WA, "body": "Your device has been replaced."}, channel="whatsapp"), False, None),
    ("LOAN-KFS-001", "pos", lambda: agent("send_partner_application_link", {"merchant_id": "M-0001"}, channel="whatsapp"), True, "BLOCK"),
    ("LOAN-KFS-001", "neg", lambda: agent("send_partner_application_link", {"merchant_id": "M-0001"}, channel="whatsapp", facts={"kfs_message_sent": True}), False, None),
    ("KYC-NO-CHAT-001", "pos", lambda: agent("send_message", {**WA, "body": "my account is 123456789012"}, channel="whatsapp"), True, "BLOCK"),
    ("KYC-NO-CHAT-001", "neg", lambda: agent("send_message", {**WA, "body": "Call support on 9876543210 any time"}, channel="whatsapp"), False, None),
    ("SECRET-001", "pos", lambda: agent("send_message", {**WA, "body": "OTP 482913"}, channel="whatsapp"), True, "BLOCK"),
    ("SECRET-001", "neg", lambda: agent("send_message", {**WA, "body": "Never share your OTP or PIN with anyone."}, channel="whatsapp"), False, None),
    ("LOAN-RATE-001", "pos", lambda: agent("send_message", {**WA, "body": "आपका लोन 12 प्रतिशत ब्याज पर तैयार है"}, channel="whatsapp"), True, "BLOCK"),
    ("LOAN-RATE-001", "neg", lambda: agent("send_message", {**WA, "body": "आपका डिवाइस बदल दिया गया है"}, channel="whatsapp"), False, None),
    ("SECRET-001", "pos", lambda: agent("send_message", {**WA, "body": "apna otp bhejo"}, channel="whatsapp"), True, "BLOCK"),
    ("SECRET-001", "neg", lambda: agent("send_message", {**WA, "body": "Paytm kabhi OTP nahi maangta"}, channel="whatsapp"), False, None),
    ("WAIVER-BAND-001", "pos", lambda: agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6}), True, "ESCALATE"),
    ("WAIVER-BAND-001", "neg", lambda: agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}), False, None),
    ("MONEY-LIMIT-001", "pos", lambda: agent("create_refund_request", {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 1_000_000}), True, "ESCALATE"),
    ("MONEY-LIMIT-001", "neg", lambda: agent("create_refund_request", {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 999_999}), False, None),
    ("RISK-HOLD-001", "pos", lambda: human("sales_lead", "clear_risk_hold", {"settlement_id": "S-0060"}), True, "ESCALATE"),
    ("RISK-HOLD-001", "neg", lambda: human("risk_reviewer", "clear_risk_hold", {"settlement_id": "S-0060"}), False, None),
    ("CONTACT-CAP-001", "pos", lambda: agent("send_message", {**WA, "body": "hi"}, channel="whatsapp", facts={"_budget": "no"}), True, "BLOCK"),
    ("CONTACT-CAP-001", "neg", lambda: agent("send_message", {**WA, "body": "hi"}, channel="whatsapp"), False, None),
    ("CONSENT-001", "pos", lambda: agent("send_message", {"merchant_id": "M-0001", "channel": "sms", "body": "hi"}, channel="sms"), True, "BLOCK"),
    ("CONSENT-001", "neg", lambda: agent("send_message", {**WA, "body": "hi"}, channel="whatsapp"), False, None),
    ("ROLE-001", "pos", lambda: human("auditor", "schedule_follow_up", {"merchant_id": "M-0001", "due_at": "2026-11-01T10:00:00+05:30"}), True, "BLOCK"),
    ("ROLE-001", "neg", lambda: human("support_executive", "schedule_follow_up", {"merchant_id": "M-0001", "due_at": "2026-11-01T10:00:00+05:30"}), False, None),
    ("CLOSE-NO-OBSTRUCT-001", "pos", lambda: agent("create_device_order", {"merchant_id": "M-0001"}, facts={"closure_requests": 2}), True, "BLOCK"),
    ("CLOSE-NO-OBSTRUCT-001", "neg", lambda: agent("create_device_order", {"merchant_id": "M-0001"}, facts={"closure_requests": 1}), False, None),
    ("MANDATE-LIMIT-001", "pos", lambda: agent("collect_mandate_payment", {"merchant_id": "M-0001", "amount_paise": 600_000}, facts=MAND), True, "ESCALATE"),
    ("MANDATE-LIMIT-001", "neg", lambda: agent("collect_mandate_payment", {"merchant_id": "M-0001", "amount_paise": 500_000}, facts=MAND), False, None),
    ("RIVAL-TARGET-001", "pos", lambda: agent("get_merchant", {"merchant_id": "M-0001"}, facts={"targeting": "individual_rival"}), True, "BLOCK"),
    ("RIVAL-TARGET-001", "neg", lambda: agent("get_merchant", {"merchant_id": "M-0001"}, facts={"targeting": "segment"}), False, None),
    ("MCP-EXPOSE-001", "pos", lambda: agent("change_bank_details", {"merchant_id": "M-0001"}, facts={"via_mcp": True}), True, "BLOCK"),
    ("MCP-EXPOSE-001", "neg", lambda: agent("get_merchant", {"merchant_id": "M-0001"}, facts={"via_mcp": True}), False, None),
    ("CONSENT-COACH-001", "pos", lambda: agent("get_merchant", {"merchant_id": "M-0001"}, facts={"analysis": True}), True, "BLOCK"),
    ("CONSENT-COACH-001", "neg", lambda: agent("get_merchant", {"merchant_id": "M-0001"}, facts={"analysis": False}), False, None),
    ("INCIDENT-HOLD-001", "pos", lambda: agent("create_device_replacement", {"merchant_id": "M-0001", "reason": "NO_ANNOUNCEMENT"}, facts={"_incident_hold": "M-0001"}), True, "BLOCK"),
    ("INCIDENT-HOLD-001", "neg", lambda: agent("create_device_replacement", {"merchant_id": "M-0001", "reason": "NO_ANNOUNCEMENT"}, facts={"_incident_hold": "M-0002"}), False, None),
]


@pytest.mark.parametrize("rule_id,kind,make,fires,severity", [pytest.param(*c, id=f"{c[0]}-{c[1]}") for c in CASES])
def test_rule_fixture(conn, clock, registry, http, rule_id, kind, make, fires, severity):
    intent = make()
    if intent.facts.get("_incident_hold"):                       # an OPEN area incident holding that merchant (jobs/incidents.py)
        from jobs.incidents import ensure_schema
        ensure_schema(conn)
        conn.execute("INSERT INTO incidents(incident_id, kind, area, status, opened_at) VALUES ('INC-T', 'HEARTBEAT_PINCODE', '560034', 'OPEN', 'x')")
        conn.execute("INSERT INTO incident_holds(merchant_id, incident_id) VALUES (?, 'INC-T')", (intent.facts["_incident_hold"],))
    budget = FakeBudget(ok=intent.facts.get("_budget") != "no", why="daily cap reached")
    eng = build_engine(conn, clock, registry, reader=make_reader(http, registry), contact_budget=budget)
    d = eng.evaluate(intent)
    hit = [v for v in d.violations if v.rule_id == rule_id]
    if fires:
        assert len(hit) == 1, [(v.rule_id, v.reason) for v in d.violations]
        assert hit[0].severity == severity and hit[0].source and hit[0].policy_hash == eng.policy_hash
    else:
        assert hit == []


def test_thirty_eight_fixtures_cover_seventeen_rules():
    assert len(CASES) == 38 and len({c[0] for c in CASES}) == 17


# ---- WAIVER-BAND-001 watch-out: the band comes from the pricing system, not the intent ----------------------------
def test_six_months_against_band_one_escalates_to_sales_lead_with_rule_id(engine):
    d = engine.evaluate(agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6}))
    v = d.violations[0]
    assert d.outcome == "ESCALATE" and v.rule_id == "WAIVER-BAND-001" and v.route == "sales_lead"
    assert "6 months requested, band 1" in v.reason


def test_waiver_band_is_read_from_the_pricing_system_and_follows_it(engine, conn):
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6})
    assert engine.evaluate(it).outcome == "ESCALATE"
    conn.execute("UPDATE offer_bands SET max_months = 6 WHERE segment = 'kirana'")        # pricing system now allows 6
    assert engine.evaluate(it).outcome == "ALLOW"
    conn.execute("UPDATE offer_bands SET max_months = 0 WHERE segment = 'kirana'")
    assert engine.evaluate(agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1})).outcome == "ESCALATE"


def test_waiver_band_ignores_a_band_stated_in_the_intent(engine):
    forged = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6, "max_months": 6, "band": 6, "offer_band": {"max_months": 6}},
                   facts={"max_months": 6, "band": 6, "offer_band": 6})
    assert engine.evaluate(forged).outcome == "ESCALATE"


def test_waiver_with_unreadable_pricing_escalates_instead_of_allowing(conn, clock, registry):
    def dead(name, args):
        raise RuntimeError("pricing down")
    eng = build_engine(conn, clock, registry, reader=dead)
    d = eng.evaluate(agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}))
    assert d.outcome == "ESCALATE" and ids(d) == {"WAIVER-BAND-001"}
    assert build_engine(conn, clock, registry).evaluate(agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1})).outcome == "ESCALATE"


# ---- MANDATE-LIMIT-001 watch-out: cumulative, not per payment ---------------------------------------------------
def test_mandate_limit_is_cumulative_per_month(engine):
    f = {"mandate_limit_paise": 500_000, "mandate_purpose": "paytm_dues"}
    pay = lambda total: agent("collect_mandate_payment", {"merchant_id": "M-0001", "amount_paise": 300_000}, facts={**f, "mandate_month_total_paise": total})
    assert engine.evaluate(pay(0)).outcome == "ALLOW"                  # each payment alone is under the limit
    d = engine.evaluate(pay(300_000))                                  # but 300k + 300k > 500k
    assert d.outcome == "ESCALATE" and ids(d) == {"MANDATE-LIMIT-001"}
    assert engine.evaluate(pay(200_000)).outcome == "ALLOW"            # exactly the limit is fine


def test_mandate_purpose_must_be_paytm_dues(engine):
    d = engine.evaluate(agent("collect_mandate_payment", {"merchant_id": "M-0001", "amount_paise": 1}, facts={**MAND, "mandate_purpose": "loan_emi"}))
    assert d.outcome == "ESCALATE" and "purpose" in d.violations[0].reason


# ---- SECRET-001 / KYC: Indic digits are normalised first ------------------------------------------------------------
def test_indic_and_fullwidth_digits_are_normalised():
    assert normalise_digits("१२३४ ४८२९१३ ١٢٣ １２３") == "1234 482913 123 123"


@pytest.mark.parametrize("body", ["आपका OTP ४८२९१३ है", "PIN: ١٢٣٤", "ＯＴＰ １２３４５６", "123456 is your OTP", "Please share your PIN"])
def test_secrets_in_any_script_are_blocked(engine, body):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert d.outcome == "BLOCK" and "SECRET-001" in ids(d), body


@pytest.mark.parametrize("body,blocked", [
    ("Paytm never asks for your PIN or OTP. Settlements amount is 3344 rupees.", False),          # the compliance line before a claim
    ("Paytm kabhi aapka PIN ya OTP nahi maangta. Aapka settlement 3344 rupees ka hai.", False),
    ("my otp. 482913", True),                                                                       # no negation: still a secret
    ("Never share your OTP. OTP 482913", True),                                                     # the warning does not hide the value
    ("Do not share it, your OTP is 482913.", True),                                                 # digits in the same sentence
])
def test_a_warning_sentence_does_not_pair_with_an_amount_in_the_next_sentence(engine, body, blocked):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert ("SECRET-001" in ids(d)) is blocked, (body, d.outcome)


@pytest.mark.parametrize("args", [{"otp": "1234"}, {"nested": {"pin": "9999"}}])
def test_secret_named_fields_are_blocked(engine, args):
    d = engine.evaluate(agent("save_preference", {"merchant_id": "M-0001", "key": "k", "value": "v", **args}))
    assert "SECRET-001" in ids(d)


def test_pin_code_of_an_address_is_not_a_secret(engine):
    d = engine.evaluate(agent("draft_message", {"merchant_id": "M-0001", "text": "Visit us, pin code 560001"}, allowlist=None))
    assert d.outcome == "ALLOW"


@pytest.mark.parametrize("args", [{"text": "acct १२३४५६७८९०१२"}, {"text": "IFSC HDFC0001234"}, {"ifsc": "x"}, {"account_number": "1"}])
def test_account_and_ifsc_data_is_blocked(engine, args):
    d = engine.evaluate(agent("draft_message", {"merchant_id": "M-0001", **args}, allowlist=None))
    assert "KYC-NO-CHAT-001" in ids(d)


# ---- SECRET-001: word order (verb before or after) and Devanagari/Kannada script words ----------------------------------------
@pytest.mark.parametrize("body", [
    "apna otp bhejo", "OTP share kijiye", "aapka OTP bata do", "Please send your OTP", "pin batao", "UPI PIN dijiye",
    "kripya apna password bhejiye", "OTP enter karke bhejiye", "your CVV, please share",
    "अपना ओटीपी बताइए", "ओटीपी भेजिए", "आपका पिन बताओ", "पासवर्ड भेजो", "सीवीवी बता दो",
    "ನಿಮ್ಮ ಒಟಿಪಿ ಹೇಳಿ", "ಒಟಿಪಿ ಕಳುಹಿಸಿ", "ಪಿನ್ ಕೊಡಿ", "ನಿಮ್ಮ ಪಿನ್ ಹೇಳಿ",
    "आपका ओटीपी 482913 है", "ನಿಮ್ಮ ಒಟಿಪಿ ೪೮೨೯೧೩",                        # script words next to digits
    "Never share it with others. Now send your OTP",                  # an earlier warning does not excuse a later request
])
def test_secret_requests_in_either_word_order_and_in_indic_script_are_blocked(engine, body):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert d.outcome == "BLOCK" and "SECRET-001" in ids(d), body


@pytest.mark.parametrize("body", [
    "Paytm never asks for your OTP", "Paytm kabhi aapka OTP nahi maangta", "OTP kisi ko mat batana", "apna PIN kisi ko share mat karo",
    "OTP kisi ko share na karein", "What is an OTP?", "OTP kya hota hai", "mera OTP nahi aaya", "Visit us, pin code 560001",
    "पेटीएम कभी आपका ओटीपी नहीं मांगता", "ओटीपी किसी को न बताएं", "ओटीपी क्या होता है", "मेरा पिन कोड 560001 है", "अपना पिन कोड बताइए",
    "ಒಟಿಪಿ ಯಾರಿಗೂ ಹೇಳಬೇಡಿ", "ಒಟಿಪಿ ಎಂದರೇನು", "ನಿಮ್ಮ ಪಿನ್ ಕೋಡ್ ಹೇಳಿ",
])
def test_benign_secret_mentions_are_allowed(engine, body):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert "SECRET-001" not in ids(d), body


# ---- LOAN-RATE-001: only the eligibility tool's numbers may be stated --------------------------------------------------
def _eligible(http):
    for i in range(1, 60):
        mid = f"M-{i:04d}"
        e = http.get(f"/mock/lender/eligibility?mid={mid}").json()
        if e["eligible"]:
            return mid, e["max_amount_paise"]
    pytest.skip("no eligible merchant in seed")


def test_loan_amount_must_equal_the_eligibility_tool(engine, http):
    mid, amt = _eligible(http)
    rupees = amt // 100
    ok = agent("send_message", {"merchant_id": mid, "channel": "whatsapp", "body": f"Partner loan up to Rs {rupees:,} may be available."}, channel="whatsapp")
    assert "LOAN-RATE-001" not in ids(engine.evaluate(ok))
    bad = agent("send_message", {"merchant_id": mid, "channel": "whatsapp", "body": f"Partner loan up to Rs {rupees * 2:,} may be available."}, channel="whatsapp")
    assert "LOAN-RATE-001" in ids(engine.evaluate(bad))


@pytest.mark.parametrize("body", ["loan at 1.5 per cent a month", "Your loan is approved", "loan EMI for 24 months", "loan of Rs 5 lakh guaranteed"])
def test_loan_rate_term_and_approval_claims_are_blocked(engine, body):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert "LOAN-RATE-001" in ids(d), body


@pytest.mark.parametrize("body", [
    "आपका लोन 12 प्रतिशत ब्याज पर मिलेगा", "लोन की किस्त 24 महीने की है", "आपका लोन मंजूर हो गया है", "लोन की गारंटी है",
    "ब्याज सिर्फ 2 फीसदी", "लोन 5 लाख रुपये तक मिलेगा", "ನಿಮ್ಮ ಸಾಲ ಮಂಜೂರು ಆಗಿದೆ", "ಸಾಲಕ್ಕೆ ಶೇಕಡಾ 12 ಬಡ್ಡಿ",
    "ಸಾಲ 24 ತಿಂಗಳು", "ಸಾಲ ಗ್ಯಾರಂಟಿ", "ಸಾಲ ಖಾತರಿ ಇದೆ", "karz pakka milega", "byaj sirf 2 fisadi", "aapka loan approve ho gaya",
])
def test_hindi_kannada_and_hinglish_loan_claims_are_blocked(engine, body):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert "LOAN-RATE-001" in ids(d), body


@pytest.mark.parametrize("body", [
    "आपका सेटलमेंट 12 प्रतिशत बढ़ा है", "आपका डिवाइस 24 महीने की वारंटी में है", "लोन के बारे में जानकारी चाहिए तो बताइए",
    "ನಿಮ್ಮ ಸೆಟ್ಲ್‌ಮೆಂಟ್ ಬಂದಿದೆ", "ಸಾಲದ ಬಗ್ಗೆ ಮಾಹಿತಿ ಬೇಕಿದ್ದರೆ ಹೇಳಿ", "pakka kal aa jayega device",
])
def test_non_loan_numbers_and_loan_enquiries_are_not_loan_claims(engine, body):
    d = engine.evaluate(agent("send_message", {**WA, "body": body}, channel="whatsapp"))
    assert "LOAN-RATE-001" not in ids(d), body


# ---- CONSENT-001 / CONSENT-COACH-001 -------------------------------------------------------------------------------------------
def test_revoked_consent_blocks(engine, conn):
    conn.execute("UPDATE consent SET status='REVOKED' WHERE subject_id='M-0001' AND channel='whatsapp'")
    d = engine.evaluate(agent("send_message", {**WA, "body": "hi"}, channel="whatsapp"))
    assert d.outcome == "BLOCK" and ids(d) == {"CONSENT-001"}


def test_lead_in_dnd_is_blocked_even_with_consent(engine, conn):
    dnd = conn.execute("SELECT l.lead_id FROM leads l JOIN dnd_registry d ON d.phone = l.phone_public LIMIT 1").fetchone()[0]
    clean = conn.execute("SELECT lead_id FROM leads WHERE phone_public NOT IN (SELECT phone FROM dnd_registry) LIMIT 1").fetchone()[0]
    for lead in (dnd, clean):
        conn.execute("INSERT OR REPLACE INTO consent VALUES (?,?,?,?,?)", (lead, "whatsapp", "GRANTED", "test", "2026-10-01T09:00:00+05:30"))
    args = {"merchant_id": "M-0001", "channel": "whatsapp", "body": "hello"}
    assert "CONSENT-001" in ids(engine.evaluate(agent("send_message", {**args, "lead_id": dnd}, channel="whatsapp")))
    assert "CONSENT-001" not in ids(engine.evaluate(agent("send_message", {**args, "lead_id": clean}, channel="whatsapp")))


def test_analysis_consent_record_unblocks_and_revocation_blocks(engine, conn):
    it = agent("get_merchant", {"merchant_id": "M-0001"}, facts={"analysis": True})
    assert engine.evaluate(it).outcome == "BLOCK"
    conn.execute("INSERT INTO consent VALUES ('M-0001','call_analysis','GRANTED','test','2026-10-01T09:00:00+05:30')")
    assert engine.evaluate(it).outcome == "ALLOW"
    conn.execute("UPDATE consent SET status='REVOKED' WHERE subject_id='M-0001' AND channel='call_analysis'")
    assert engine.evaluate(it).outcome == "BLOCK"


# ---- RIVAL-TARGET-001, RISK-HOLD-001, MONEY-LIMIT-001, CLOSE-NO-OBSTRUCT-001 -----------------------------------------------------------
def test_rival_targeting_is_allowed_only_when_the_legal_flag_is_on(conn, clock, registry):
    lim = load_limits(LIMITS_PATH)
    assert lim["flags"]["legal_approval"] is False
    it = agent("get_merchant", {"merchant_id": "M-0001"}, facts={"targeting": "individual_rival"})
    assert build_engine(conn, clock, registry, limits=lim).evaluate(it).outcome == "BLOCK"
    on = copy.deepcopy(lim)
    on["flags"]["legal_approval"] = True
    assert build_engine(conn, clock, registry, limits=on).evaluate(it).outcome == "ALLOW"


def test_risk_hold_follows_the_live_settlement_status(engine, conn):
    it = human("sales_lead", "clear_risk_hold", {"settlement_id": "S-0060"})
    d = engine.evaluate(it)
    assert next(v for v in d.violations if v.rule_id == "RISK-HOLD-001").route == "risk_reviewer"
    conn.execute("UPDATE settlements SET status='SETTLED' WHERE settlement_id='S-0060'")
    assert "RISK-HOLD-001" not in ids(engine.evaluate(it))


def test_money_limit_comes_from_limits_yaml_and_uses_the_settlement_amount(engine, conn, clock, registry):
    lim = load_limits(LIMITS_PATH)
    assert lim["money_limit"]["threshold_paise"] == 1_000_000
    lim["money_limit"]["threshold_paise"] = 2_000_000
    eng = build_engine(conn, clock, registry, limits=lim)
    assert "MONEY-LIMIT-001" not in ids(eng.evaluate(agent("create_refund_request", {"merchant_id": "M-0001", "payment_id": "P", "amount_paise": 1_500_000})))
    assert "MONEY-LIMIT-001" in ids(engine.evaluate(agent("clear_risk_hold", {"settlement_id": "S-0060"})))  # 1,240,000 paise held


def test_closure_obstruction_lets_closure_reads_and_drafts_through(engine):
    f = {"closure_requests": 3}
    assert engine.evaluate(agent("create_closure_request", {"merchant_id": "M-0001"}, facts=f)).outcome == "ALLOW"
    assert engine.evaluate(agent("get_plan", {"merchant_id": "M-0001"}, facts=f, allowlist=None)).outcome == "ALLOW"
    assert engine.evaluate(agent("send_message", {**WA, "body": "How about a waiver?"}, channel="whatsapp", facts=f)).outcome == "BLOCK"


def test_no_magic_numbers_in_rules():
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent.joinpath("packs/paytm_merchant/policy/rules.py").read_text()
    code = [l for l in src.splitlines() if not l.strip().startswith(('"""', "#"))]
    assert not [l for l in code if re.search(r"\b(10_?000|1_?000_?000|100_?000)\b", l) and "lakh" not in l.lower() and "LAKH" not in l]
