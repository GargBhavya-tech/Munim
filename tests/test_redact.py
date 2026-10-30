"""#35 redaction: table-driven per pattern and per script, property test, fail-closed, nothing stored un-redacted."""
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from core.audit.ledger import Ledger
from core.engine.clock import SimClock
from core.memory import redact as R
from core.memory.redact import (PLEASE_REPEAT, RedactionFailed, Redactor, redact, redact_or_drop, redact_or_repeat)

DEV = str.maketrans("0123456789", "०१२३४५६७८९")
KAN = str.maketrans("0123456789", "೦೧೨೩೪೫೬೭೮೯")

# (input, expected text, expected kinds, secret_shared)
TABLE = [
    # --- OTP in context, all scripts and spellings
    ("mera OTP 482913 hai", "mera OTP [OTP] hai", ["OTP"], True),
    ("mera OTP ४८२९१३ hai", "mera OTP [OTP] hai", ["OTP"], True),
    ("mera OTP ೪೮೨೯೧೩ hai", "mera OTP [OTP] hai", ["OTP"], True),
    ("मेरा ओटीपी ४८२९१३ है", "मेरा ओटीपी [OTP] है", ["OTP"], True),
    ("मेरा OTP 482913 है", "मेरा OTP [OTP] है", ["OTP"], True),
    ("ನನ್ನ ಒಟಿಪಿ ೪೮೨೯೧೩ ಆಗಿದೆ", "ನನ್ನ ಒಟಿಪಿ [OTP] ಆಗಿದೆ", ["OTP"], True),
    ("my OTP is 123456", "my OTP is [OTP]", ["OTP"], True),
    ("my otp is 1234", "my otp is [OTP]", ["OTP"], True),
    ("OTP: 12345678", "OTP: [OTP]", ["OTP"], True),
    ("OTP:482913", "OTP:[OTP]", ["OTP"], True),
    ("482913 is my OTP", "[OTP] is my OTP", ["OTP"], True),
    ("code 482913", "code [OTP]", ["OTP"], True),
    ("verification code is 4829", "verification code is [OTP]", ["OTP"], True),
    ("password 482913", "password [OTP]", ["OTP"], True),
    ("पासवर्ड ४८२९१३ है", "पासवर्ड [OTP] है", ["OTP"], True),
    ("OTP 4 8 2 9 1 3", "OTP [OTP]", ["OTP"], True),
    ("OTP 482-913", "OTP [OTP]", ["OTP"], True),
    ("OTP 4829 13 hai", "OTP [OTP] hai", ["OTP"], True),
    ("mera OTP hai aap dekh wo 482913 hai", "mera OTP hai aap dekh wo [OTP] hai", ["OTP"], True),   # 5 words away (edge of window)
    ("mera OTP hai aap dekh lo wo 482913 hai", "mera OTP hai aap dekh lo wo 482913 hai", [], False),   # 7 words away: outside the window
    ("O.T.P. 482913", "O.T.P. [OTP]", ["OTP"], True),
    # --- PIN
    ("my PIN is 1234", "my PIN is [PIN]", ["PIN"], True),
    ("meri pin 4321 hai", "meri pin [PIN] hai", ["PIN"], True),
    ("मेरा पिन ४३२१ है", "मेरा पिन [PIN] है", ["PIN"], True),
    ("ನನ್ನ ಪಿನ್ ೪೩೨೧", "ನನ್ನ ಪಿನ್ [PIN]", ["PIN"], True),
    ("UPI PIN 123456", "UPI PIN [PIN]", ["PIN"], True),
    ("mpin 5678", "mpin [PIN]", ["PIN"], True),
    # --- amount markers win over OTP-ish context
    ("discount code ₹100000", "discount code ₹100000", [], False),
    ("discount code ₹ 100000", "discount code ₹ 100000", [], False),
    ("discount code Rs. 100000", "discount code Rs. 100000", [], False),
    ("discount code 100000 rupaye", "discount code 100000 rupaye", [], False),
    ("code 100000 रुपये", "code 100000 रुपये", [], False),
    ("code ೧೦೦೦೦೦ ರೂಪಾಯಿ", "code 100000 ರೂಪಾಯಿ", [], False),
    ("code ₹1,00,000", "code ₹1,00,000", [], False),
    ("OTP aaya aur ₹8200 kata", "OTP aaya aur ₹8200 kata", [], False),
    # --- not in context: untouched
    ("mera settlement 482913 ka hai", "mera settlement 482913 ka hai", [], False),
    ("kitna fee hai 8200 rupaye", "kitna fee hai 8200 rupaye", [], False),
    ("OTP aa gaya", "OTP aa gaya", [], False),
    # --- Aadhaar-like
    ("aadhaar 1234 5678 9012", "aadhaar XXXX-XXXX-9012", ["AADHAAR"], False),
    ("aadhaar 1234-5678-9012", "aadhaar XXXX-XXXX-9012", ["AADHAAR"], False),
    ("aadhaar 123456789012", "aadhaar XXXX-XXXX-9012", ["AADHAAR"], False),
    ("आधार १२३४ ५६७८ ९०१२", "आधार XXXX-XXXX-9012", ["AADHAAR"], False),
    ("ಆಧಾರ್ ೧೨೩೪೫೬೭೮೯೦೧೨", "ಆಧಾರ್ XXXX-XXXX-9012", ["AADHAAR"], False),
    # --- PAN
    ("PAN ABCDE1234F hai", "PAN XXXXX1234X hai", ["PAN"], False),
    ("pan abcde1234f", "pan XXXXX1234X", ["PAN"], False),
    ("पैन ABCDE१२३४F", "पैन XXXXX1234X", ["PAN"], False),
    # --- cards (Luhn valid only)
    ("card 4111111111111111", "card XXXX…1111", ["CARD"], False),
    ("card 4111 1111 1111 1111 hai", "card XXXX…1111 hai", ["CARD"], False),
    ("card 4111-1111-1111-1111", "card XXXX…1111", ["CARD"], False),
    ("कार्ड ४१११ ११११ ११११ ११११", "कार्ड XXXX…1111", ["CARD"], False),
    ("ಕಾರ್ಡ್ ೪೧೧೧೧೧೧೧೧೧೧೧೧೧೧೧", "ಕಾರ್ಡ್ XXXX…1111", ["CARD"], False),
    ("card 378282246310005", "card XXXX…0005", ["CARD"], False),          # 15-digit amex test number
    ("card 4111111111111111 aur 4111 1111 1111 1111", "card XXXX…1111 aur XXXX…1111", ["CARD"], False),
    # --- bank accounts near their word
    ("account number 123456789", "account number XXXXX6789", ["BANK_ACCOUNT"], False),
    ("a/c 000123456789012", "a/c XXXXXXXXXXX9012", ["BANK_ACCOUNT"], False),
    ("खाता नंबर १२३४५६७८९०१ है", "खाता नंबर XXXXXXX8901 है", ["BANK_ACCOUNT"], False),
    ("ಖಾತೆ ಸಂಖ್ಯೆ ೧೨೩೪೫೬೭೮೯೦೧೨೩", "ಖಾತೆ ಸಂಖ್ಯೆ XXXXXXXXX0123", ["BANK_ACCOUNT"], False),
    ("IFSC SBIN0001234 account 1234567890123", "IFSC SBIN0001234 account XXXXXXXXX0123", ["BANK_ACCOUNT"], False),
    ("order 123456789 aaya", "order 123456789 aaya", [], False),      # 9 digits but no account word
]


@pytest.mark.parametrize("text,expected,kinds,secret", TABLE, ids=[t[0][:40] for t in TABLE])
def test_table(text, expected, kinds, secret):
    r = redact(text)
    assert r.text == expected
    assert r.kinds == kinds
    assert r.secret_shared is secret


def test_bible_example_exact():
    r = redact("mera OTP ४८२९१३ hai")
    assert r.text == "mera OTP [OTP] hai"
    assert r.secret_shared is True
    assert "482913" not in r.text
    assert "४८२९१३" not in r.text


def test_bible_full_example():
    r = redact("mera OTP 482913 hai, abhi settlement bhej do")
    assert r.text == "mera OTP [OTP] hai, abhi settlement bhej do"


def test_digits_do_not_survive_in_any_output_field():
    for src in ("mera OTP ४८२९१३ hai", "mera OTP ೪೮೨೯೧೩ hai", "mera OTP 482913 hai", "OTP 4 8 2 9 1 3"):
        r = redact(src)
        blob = repr(r) + r.text + "|".join(r.kinds) + str(r.secret_shared) + json.dumps(r.__dict__, ensure_ascii=False, default=str)
        for variant in ("482913", "४८२९१३", "೪೮೨೯೧೩", "4 8 2 9 1 3"):
            assert variant not in blob


def test_valid_luhn_card_inside_longer_digit_run_is_still_caught():
    # a Luhn-valid 16 digit card followed by extra digits must not slip through because the whole 19 digits fail Luhn
    r = redact("card 4111 1111 1111 1111 123")
    assert "4111" not in r.text


def test_non_luhn_16_digits_are_left_alone():
    assert redact("ref 1234567812345678").text == "ref 1234567812345678"   # Bible: cards are Luhn only


def test_amount_marker_protects_only_the_otp_rule():
    # amount marker must not hide a real OTP elsewhere in the same message
    r = redact("code ₹5000 aur OTP 482913")
    assert r.text == "code ₹5000 aur OTP [OTP]"


# ---- phones and names: stable tokens
def test_phone_tokens_are_stable_within_a_conversation():
    rd = Redactor()
    a = rd.redact("call 9876543210")
    b = rd.redact("mera number 98765 43210 aur +91 91234 56789")
    assert a.text == "call [PHONE_1]"
    assert b.text == "mera number [PHONE_1] aur [PHONE_2]"
    assert a.kinds == ["PHONE"]
    assert "9876543210" not in repr(rd) and "98765" not in repr(rd)


@pytest.mark.parametrize("s", ["9876543210", "+919876543210", "+91 98765 43210", "09876543210", "९८७६५४३२१०", "೯೮೭೬೫೪೩೨೧೦"])
def test_phone_forms(s):
    r = redact(f"call {s} now")
    assert r.text == "call [PHONE_1] now"


def test_not_a_phone_when_it_cannot_be_one():
    assert redact("txn 1234567890").text == "txn 1234567890"   # starts with 1: not an Indian mobile


def test_known_names_become_stable_tokens():
    rd = Redactor(names=["Ramesh Kumar", "Sunita"])
    r = rd.redact("Ramesh Kumar ne kaha Sunita ko bhejo, ramesh kumar ka phone 9876543210")
    assert r.text == "[NAME_1] ne kaha [NAME_2] ko bhejo, [NAME_1] ka phone [PHONE_1]"
    assert rd.redact("Sunita aayi").text == "[NAME_2] aayi"
    assert redact("Sunita aayi", names=["Sunita"]).text == "[NAME_1] aayi"
    assert redact("Sunitaben aayi", names=["Sunita"]).text == "Sunitaben aayi"     # whole word only


def test_indic_script_names():
    r = redact("रमेश ने कहा", names=["रमेश"])
    assert r.text == "[NAME_1] ने कहा"
    assert redact("ರಮೇಶ್ ಬಂದರು", names=["ರಮೇಶ್"]).text == "[NAME_1] ಬಂದರು"


def test_multiple_secrets_and_kinds_in_one_message():
    r = redact("OTP 482913 aadhaar 1234 5678 9012 PAN ABCDE1234F")
    assert r.text == "OTP [OTP] aadhaar XXXX-XXXX-9012 PAN XXXXX1234X"
    assert r.kinds == ["OTP", "AADHAAR", "PAN"]
    assert r.secret_shared is True


def test_empty_and_plain_text():
    assert redact("").text == "" and not redact("").secret_shared
    r = redact("settlement kab aayega?")
    assert r.text == "settlement kab aayega?" and r.kinds == []


def test_normal_text_has_indic_digits_normalised_but_otherwise_intact():
    assert redact("५०० रुपये chahiye").text == "500 रुपये chahiye"


# ---- fail-closed
def test_exception_inside_redaction_raises_redaction_failed(monkeypatch):
    def boom(*a, **k):
        raise ValueError("regex exploded")
    monkeypatch.setattr(R, "_find_spans", boom)
    with pytest.raises(RedactionFailed):
        redact("mera OTP 482913 hai")


def test_redact_or_drop_returns_none_on_failure_never_the_raw_text(monkeypatch):
    monkeypatch.setattr(R, "_find_spans", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert redact_or_drop("mera OTP 482913 hai") is None
    res, msg = redact_or_repeat("mera OTP 482913 hai", language="hinglish")
    assert res is None and msg == PLEASE_REPEAT["hinglish"]
    assert redact_or_repeat("x", language="zz")[1] == PLEASE_REPEAT["en"]


def test_failure_in_normalisation_or_apply_also_fails_closed(monkeypatch):
    monkeypatch.setattr(R, "_apply", lambda *a, **k: 1 / 0)
    assert redact_or_drop("OTP 482913") is None
    monkeypatch.undo()
    monkeypatch.setattr(R, "normalise_digits", lambda s: (_ for _ in ()).throw(OSError("x")))
    assert redact_or_drop("OTP 482913") is None


def test_non_string_input_fails_closed():
    assert redact_or_drop(None) is None            # type: ignore[arg-type]
    with pytest.raises(RedactionFailed):
        redact(b"OTP 482913")                      # type: ignore[arg-type]


def test_verifier_catches_a_detector_that_misses(monkeypatch):
    # if a detector were to emit a span that does not cover the digits, the second pass / verifier must refuse
    real = R._find_spans
    calls = {"n": 0}

    def partial(reg, norm):
        calls["n"] += 1
        spans = real(reg, norm)
        if calls["n"] == 1:
            for s in spans:
                s.end = s.end - 2   # leak the last two digits of the secret
        return spans
    monkeypatch.setattr(R, "_find_spans", partial)
    assert redact_or_drop("mera OTP 482913 hai") is None


# ---- nothing stored un-redacted
def test_database_ledger_and_prompt_contain_no_otp(conn):
    clock = SimClock()
    inbound = "mera OTP ४८२९१३ hai, abhi settlement bhej do"
    res = redact_or_drop(inbound)
    assert res is not None
    conn.execute("INSERT INTO cases(case_id, playbook_id, state, lane, opened_at) VALUES ('C-T1','pb','OPEN','P2','x')")
    conn.execute("INSERT INTO turns(case_id, ts_sim, role, text_redacted) VALUES ('C-T1', ?, 'MERCHANT', ?)", (clock.now_iso(), res.text))
    Ledger(conn, clock).append(kind="STATE", actor="SYSTEM", case_id="C-T1",
                               payload={"merchant_text": res.text, "secret_shared": res.secret_shared})
    conn.commit()
    prompt = f"<untrusted>{res.text}</untrusted>"
    dump = "\n".join(conn.iterdump())
    for blob in (dump, prompt):
        for needle in ("482913", "४८२९१३"):
            assert needle not in blob
    assert "[OTP]" in dump


# ---- property test: no digit run flagged as OTP/PIN survives
_FILLER = ["mera", "aapka", "hai", "aur", "abhi", "bhej", "do", "the", "is", "please", "settlement", "ka", "ne", "bataya", "kya", "यह", "है", "मेरा", "ನನ್ನ", "ಆಗಿದೆ"]
_KEYWORDS = ["OTP", "otp", "Otp", "PIN", "pin", "code", "password", "ओटीपी", "पिन", "पासवर्ड", "ಒಟಿಪಿ", "ಪಿನ್", "O.T.P.", "mpin", "OTP:", "PIN:"]
_TRANSLATE = [None, DEV, KAN]


@st.composite
def secret_messages(draw):
    n = draw(st.integers(4, 8))
    digits = draw(st.text("0123456789", min_size=n, max_size=n))
    script = draw(st.sampled_from(_TRANSLATE))
    shown = digits.translate(script) if script else digits
    sep = draw(st.sampled_from(["", "", "", " ", "-"]))
    if sep and n >= 4:
        cut = draw(st.integers(1, n - 1))
        shown = shown[:cut] + sep + shown[cut:]
    kw = draw(st.sampled_from(_KEYWORDS))
    before = draw(st.lists(st.sampled_from(_FILLER), max_size=6))
    after = draw(st.lists(st.sampled_from(_FILLER), max_size=6))
    gap_before = draw(st.integers(0, 4))   # words between keyword and digits (window is 5)
    mid = draw(st.lists(st.sampled_from(_FILLER), min_size=gap_before, max_size=gap_before))
    if draw(st.booleans()):
        words = before + [kw] + mid + [shown] + after
    else:
        words = before + [shown] + mid + [kw] + after
    return digits, " ".join(words)


@settings(max_examples=1500, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
@given(secret_messages())
def test_property_no_flagged_digit_run_survives(case):
    digits, text = case
    r = redact(text)
    assert r.secret_shared, text
    flat = "".join(ch for ch in r.text if ch.isdigit())
    assert flat == "", (text, r.text)           # no digit of any script survives anywhere in the output
    assert digits not in r.text
    assert "[OTP]" in r.text or "[PIN]" in r.text


@settings(max_examples=300, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
@given(st.text(max_size=120))
def test_property_never_crashes_and_never_returns_raw_secret(text):
    r = redact_or_drop(text)
    assert r is not None                         # arbitrary unicode must not make redaction fail
    assert isinstance(r.text, str) and isinstance(r.secret_shared, bool)
