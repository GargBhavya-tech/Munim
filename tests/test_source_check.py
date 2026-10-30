"""#43 source check + numbers check: a claim's numbers must appear in THE source it cites."""
import pytest

from core.gate.sources import Claim, ClaimCheck, SourceBook, check_claim, check_claims

TOOL = "tool:get_settlements#call_17"
KYC = "tool:get_kyc_status#call_18"
KB = "kb:fees_faq#device_fee@v1"
POL = "policy:WAIVER-BAND-001"


@pytest.fixture
def book():
    b = SourceBook()
    b.add_tool_result(TOOL, {"settlement_id": "ST-0042", "status": "HELD_RISK", "amount": 8200.00, "amount_paise": 820000,
                             "expected_on": "2026-10-30", "rows": [{"fee": 118}, {"fee": 236}]})
    b.add_tool_result(KYC, {"step": "document_upload", "docs_pending": 2, "attempts": 3, "verified": False, "note": None})
    b.add_kb(KB, "The soundbox rental is ₹149 per month. Replacement within 7 working days.")
    b.add_policy(POL, {"id": "WAIVER-BAND-001", "max_waiver_months": 3, "text": "Waiver up to 3 months."})
    return b


def C(text, source=TOOL, cid="c1"):
    return Claim(cid, text, source)


def test_correct_claim_passes(book):
    r = check_claim(C("Your settlement of ₹8,200 is under review."), book)
    assert (r.source_exists, r.numbers_ok, r.unmatched_numbers, r.reason) == (True, True, [], "ok")
    assert r.claim_id == "c1" and r.ok


def test_invented_fee_fails(book):
    r = check_claim(C("The fee is ₹99.", KB), book)
    assert r.source_exists and not r.numbers_ok and not r.ok
    assert r.unmatched_numbers == ["99"]
    assert r.reason == "numbers_not_in_source"


def test_correct_kb_claim_passes(book):
    assert check_claim(C("Rental is ₹149 a month.", KB), book).ok


def test_number_present_only_in_a_different_source_fails(book):
    # 149 is in the KB chunk, not in the settlement tool result that this claim cites
    r = check_claim(C("Rental is ₹149 a month.", TOOL), book)
    assert r.source_exists and not r.numbers_ok and r.unmatched_numbers == ["149"]
    # and the other way round
    assert not check_claim(C("Settlement is ₹8,200.", KB), book).numbers_ok


def test_number_elsewhere_in_the_same_turn_does_not_count(book):
    claims = [C("Settlement is ₹8,200.", TOOL, "c1"), C("Two docs are pending, 8200 too.", KYC, "c2")]
    checks = check_claims(claims, book)
    assert [c.ok for c in checks] == [True, False]
    assert checks[1].unmatched_numbers == ["8200"]


def test_nonexistent_source_fails(book):
    r = check_claim(C("Your KYC is stuck.", "tool:get_kyc_status#call_99"), book)
    assert not r.source_exists and not r.ok and r.reason == "source_not_found"


def test_source_prefix_must_match_the_store(book):
    # same key text but wrong namespace: a KB id that is not in kb_chunks, and a policy id used as a tool
    assert not check_claim(C("x", "kb:" + TOOL[5:]), book).source_exists
    assert not check_claim(C("x", "policy:get_settlements#call_17"), book).source_exists
    assert not check_claim(C("x", "FIX-FIRST-001"), book).source_exists


def test_nonexistent_source_reports_numbers_unverified(book):
    r = check_claim(C("It costs ₹99.", "kb:nope#nothing@v9"), book)
    assert not r.source_exists and not r.numbers_ok and r.unmatched_numbers == ["99"]


def test_8200_formats_are_equal(book):
    for t in ("₹8,200", "8200", "Rs. 8200.00", "8,200.00", "₹8200", "8.2k", "8 हज़ार 2 सौ"):
        r = check_claim(C(f"Amount {t}"), book)
        # "8 हज़ार 2 सौ" is two numbers (8000, 200) and must NOT be accepted
        assert r.numbers_ok is (t != "8 हज़ार 2 सौ"), t


def test_decimal_source_value_matches_integer_claim_and_vice_versa():
    b = SourceBook()
    b.add_tool_result("tool:a#1", {"amount": "8200.00"})
    b.add_tool_result("tool:b#1", {"amount": 8200})
    assert check_claim(C("₹8,200", "tool:a#1"), b).ok
    assert check_claim(C("₹8,200.00", "tool:b#1"), b).ok
    assert not check_claim(C("₹8,201", "tool:a#1"), b).ok
    assert not check_claim(C("₹8,200.01", "tool:a#1"), b).ok


def test_paise_fields_are_read_as_rupees(book):
    b = SourceBook()
    b.add_tool_result("tool:c#1", {"max_amount_paise": 5000000})
    assert check_claim(C("You can borrow up to ₹50,000", "tool:c#1"), b).ok
    assert not check_claim(C("You can borrow up to ₹5,000", "tool:c#1"), b).ok


def test_claim_with_no_numbers_passes_numbers_check(book):
    r = check_claim(C("Your KYC stopped at document upload.", KYC), book)
    assert r.ok and r.numbers_ok and r.unmatched_numbers == []


def test_claim_with_no_numbers_still_needs_its_source(book):
    assert not check_claim(C("Your KYC stopped.", "tool:missing#1"), book).ok


def test_indic_digit_claims(book):
    assert check_claim(C("आपका ₹८,२०० का सेटलमेंट रुका है", TOOL), book).ok
    assert check_claim(C("ನಿಮ್ಮ ೮೨೦೦ ರೂಪಾಯಿ", TOOL), book).ok
    assert not check_claim(C("आपका ₹८,२०१ का सेटलमेंट", TOOL), book).ok


def test_word_form_claims(book):
    assert check_claim(C("There are two documents pending", KYC), book).ok
    assert check_claim(C("eight thousand two hundred rupees", TOOL), book).ok
    assert not check_claim(C("nine thousand rupees", TOOL), book).ok
    assert check_claim(C("आठ हज़ार दो सौ रुपये", TOOL), book).ok
    assert check_claim(C("ಎಂಟು ಸಾವಿರ", TOOL), book).unmatched_numbers == ["ಎಂಟು ಸಾವಿರ"]  # 8000 is not 8200


def test_word_form_amount_must_be_equal_not_just_present(book):
    assert not check_claim(C("ek lakh rupaye", TOOL), book).ok


def test_every_number_in_the_claim_must_match(book):
    r = check_claim(C("₹8,200 held for 7 days and 3 attempts", TOOL), book)
    assert not r.numbers_ok and r.unmatched_numbers == ["7", "3"]
    assert check_claim(C("3 attempts and 2 docs pending", KYC), book).ok
    assert not check_claim(C("3 attempts and 5 docs pending", KYC), book).ok


def test_dates_must_match_after_normalisation(book):
    assert check_claim(C("Expected on 30 October 2026", TOOL), book).ok
    assert check_claim(C("Expected on 30/10/2026", TOOL), book).ok
    assert not check_claim(C("Expected on 31 October 2026", TOOL), book).ok


def test_policy_and_nested_values(book):
    assert check_claim(C("Waiver up to 3 months", POL), book).ok
    assert not check_claim(C("Waiver up to 6 months", POL), book).ok
    assert check_claim(C("Fees were 118 and 236", TOOL), book).ok  # nested list of dicts


def test_digits_inside_identifier_strings_do_not_justify_numbers(book):
    # settlement_id "ST-0042" must not make "42" or "0042" a grounded number
    assert not check_claim(C("There are 42 settlements", TOOL), book).numbers_ok
    b = SourceBook()
    b.add_tool_result("tool:t#1", {"ref": "call_17", "ifsc": "SBIN0001234", "text": "Held 17 days"})
    assert check_claim(C("Held 17 days", "tool:t#1"), b).ok            # real prose number still counts
    assert not check_claim(C("fee 1234", "tool:t#1"), b).ok


def test_booleans_and_none_are_not_numbers(book):
    assert not check_claim(C("There is 1 pending", KYC), book).numbers_ok  # verified False must not become 0/1


def test_check_claims_returns_one_result_per_claim_in_order(book):
    out = check_claims([C("a", TOOL, "c1"), C("b", "tool:x#1", "c2")], book)
    assert [(c.claim_id, c.source_exists) for c in out] == [("c1", True), ("c2", False)]
    assert all(isinstance(c, ClaimCheck) for c in out)
