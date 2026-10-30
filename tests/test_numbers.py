"""#42 number normalisation: table of forms (digits, Indic digits, number words in en/hi/kn/hinglish, money, dates, times)."""
from decimal import Decimal

import pytest

from core.gate.numbers import extract_numbers, normalise_digits, to_paise

# (text, kind, value)  -- money value is integer paise
ROWS = [
    # --- plain digit forms equal to 2000
    ("2000", "plain", 2000),
    ("2,000", "plain", 2000),
    ("2k", "plain", 2000),
    ("2K", "plain", 2000),
    ("2 thousand", "plain", 2000),
    ("2 hazaar", "plain", 2000),
    ("2 हज़ार", "plain", 2000),
    ("2 ಸಾವಿರ", "plain", 2000),
    ("२०००", "plain", 2000),
    ("೨೦೦೦", "plain", 2000),
    ("२,०००", "plain", 2000),
    # --- number words that mean 2000
    ("do hazaar", "plain", 2000),
    ("दो हज़ार", "plain", 2000),
    ("दो हजार", "plain", 2000),
    ("ಎರಡು ಸಾವಿರ", "plain", 2000),
    ("two thousand", "plain", 2000),
    # --- money
    ("₹2,000", "money", 200000),
    ("Rs 2000", "money", 200000),
    ("Rs. 2,000", "money", 200000),
    ("INR 2000", "money", 200000),
    ("2000 rupaye", "money", 200000),
    ("2000 रुपये", "money", 200000),
    ("2000 ರೂಪಾಯಿ", "money", 200000),
    ("₹२,०००", "money", 200000),
    ("₹8,200", "money", 820000),
    ("₹8,200.50", "money", 820050),
    ("₹99", "money", 9900),
    ("₹0.50", "money", 50),
    ("₹2k", "money", 200000),
    ("do hazaar rupaye", "money", 200000),
    ("दो हज़ार रुपये", "money", 200000),
    # --- lakh / crore
    ("1 lakh", "plain", 100000),
    ("1 लाख", "plain", 100000),
    ("1 ಲಕ್ಷ", "plain", 100000),
    ("ek lakh", "plain", 100000),
    ("एक लाख", "plain", 100000),
    ("1.5 lakh", "plain", 150000),
    ("1.5 लाख", "plain", 150000),
    ("1.5 ಲಕ್ಷ", "plain", 150000),
    ("2.5 lakh", "plain", 250000),
    ("0.5 lakh", "plain", 50000),
    ("₹1.5 lakh", "money", 15000000),
    ("dedh lakh", "plain", 150000),
    ("डेढ़ लाख", "plain", 150000),
    ("dhai hazaar", "plain", 2500),
    ("ढाई हज़ार", "plain", 2500),
    ("1 crore", "plain", 10000000),
    ("1 करोड़", "plain", 10000000),
    ("1 ಕೋಟಿ", "plain", 10000000),
    ("एक करोड़", "plain", 10000000),
    ("do crore", "plain", 20000000),
    ("1.2 crore", "plain", 12000000),
    ("ek lakh pachas hazaar", "plain", 150000),
    # --- Indian and western grouping
    ("1,50,000", "plain", 150000),
    ("12,34,567", "plain", 1234567),
    ("1,00,00,000", "plain", 10000000),
    ("1,500,000", "plain", 1500000),
    ("₹1,50,000", "money", 15000000),
    ("१,५०,०००", "plain", 150000),
    # --- number words, other values
    ("paanch sau", "plain", 500),
    ("पाँच सौ", "plain", 500),
    ("ಐದು ನೂರು", "plain", 500),
    ("teen hazaar paanch sau", "plain", 3500),
    ("तीन हज़ार पाँच सौ", "plain", 3500),
    ("ಮೂರು ಸಾವಿರ", "plain", 3000),
    ("ಹತ್ತು ಸಾವಿರ", "plain", 10000),
    ("पचास", "plain", 50),
    ("पच्चीस", "plain", 25),
    ("निन्यानवे", "plain", 99),
    ("pachas hazaar", "plain", 50000),
    ("ಐವತ್ತು ಸಾವಿರ", "plain", 50000),
    ("ಇಪ್ಪತ್ತು", "plain", 20),
    ("ಹದಿನೈದು", "plain", 15),
    ("twenty five", "plain", 25),
    ("ninety nine", "plain", 99),
    ("ten thousand", "plain", 10000),
    ("sau", "plain", 100),
    ("सौ", "plain", 100),
    # --- counts
    ("3 days", "count", 3),
    ("3 din", "count", 3),
    ("३ दिन", "count", 3),
    ("೩ ದಿನ", "count", 3),
    ("teen hazaar din", "count", 3000),
    ("2 documents", "count", 2),
    ("10 working days", "plain", 10),
    # --- decimals
    ("8200.00", "plain", 8200),
    ("1.5", "plain", Decimal("1.5")),
    # --- dates (ISO) and times (24 h)
    ("15 August 2026", "date", "2026-08-15"),
    ("15 Aug 2026", "date", "2026-08-15"),
    ("August 15, 2026", "date", "2026-08-15"),
    ("2026-10-29", "date", "2026-10-29"),
    ("29/10/2026", "date", "2026-10-29"),
    ("29-10-2026", "date", "2026-10-29"),
    ("5 March", "date", "--03-05"),
    ("२९/१०/२०२६", "date", "2026-10-29"),
    ("5:30 pm", "time", "17:30"),
    ("5:30 PM", "time", "17:30"),
    ("12 am", "time", "00:00"),
    ("12 pm", "time", "12:00"),
    ("9 am", "time", "09:00"),
    ("17:30", "time", "17:30"),
    ("09:05", "time", "09:05"),
    ("११:४५ am", "time", "11:45"),
]


@pytest.mark.parametrize("text,kind,value", ROWS, ids=[r[0] for r in ROWS])
def test_single_form(text, kind, value):
    got = extract_numbers(text)
    assert len(got) == 1, got
    assert (got[0].kind, got[0].value) == (kind, value)
    a, b = got[0].span
    assert text[a:b] == got[0].raw and got[0].raw.strip() != ""


def test_table_has_at_least_60_rows():
    assert len(ROWS) >= 60


def test_all_2000_forms_agree_numerically():
    forms = ["2000", "2,000", "2k", "do hazaar", "दो हज़ार", "ಎರಡು ಸಾವಿರ", "२०००", "೨೦೦೦"]
    assert {extract_numbers(f)[0].units for f in forms} == {Decimal(2000)}


# ---- the two named "watch out" mistakes
def test_one_point_five_lakh_is_150000_not_15000():
    assert extract_numbers("1.5 lakh")[0].value == 150000


def test_indian_grouping_is_not_misparsed():
    nums = extract_numbers("1,50,000")
    assert [n.value for n in nums] == [150000]          # one number, not 1 / 50 / 0
    assert [n.value for n in extract_numbers("12,34,567")] == [1234567]


def test_lakh_is_exactly_100000_and_crore_is_ten_million():
    assert extract_numbers("lakh")[0].value == 100000
    assert extract_numbers("crore")[0].value == 10_000_000


# ---- behaviours around the forms
def test_normalise_digits_maps_all_scripts():
    assert normalise_digits("०१२३४५६७८९ ೦೧೨೩೪೫೬೭೮೯ 0123456789 abc") == "0123456789 0123456789 0123456789 abc"


def test_money_is_integer_paise_never_float():
    n = extract_numbers("₹8,200.75")[0]
    assert n.kind == "money" and n.value == 820075 and isinstance(n.value, int)


def test_to_paise():
    assert to_paise(8200) == 820000
    assert to_paise("8200.50") == 820050
    assert to_paise(Decimal("0.005")) == 1  # half up
    assert to_paise(99.99) == 9999
    with pytest.raises(TypeError):
        to_paise(True)


def test_ordinary_hinglish_do_is_not_a_number():
    assert extract_numbers("abhi settlement bhej do") == []
    assert extract_numbers("ek minute ruko") == []
    assert extract_numbers("मुझे पैसे दे दो") == []


def test_one_only_counts_when_asked():
    assert extract_numbers("one moment") == []
    assert extract_numbers("one document", english_one=True)[0].value == 1


def test_several_numbers_in_a_sentence_with_spans():
    text = "Settlement of ₹8,200 on 29 Oct 2026 at 5:30 pm, 3 days late"
    got = extract_numbers(text)
    assert [(n.kind, n.value) for n in got] == [("money", 820000), ("date", "2026-10-29"), ("time", "17:30"), ("count", 3)]
    for n in got:
        assert text[n.span[0]:n.span[1]] == n.raw


def test_hindi_danda_does_not_glue_to_number_word():
    assert [n.value for n in extract_numbers("kul दो हज़ार।")] == [2000]


def test_digits_attached_to_letters_are_still_extracted_so_nothing_slips_through():
    assert [n.value for n in extract_numbers("fee Rs99")] == [9900]


def test_word_run_does_not_cross_punctuation():
    assert [n.value for n in extract_numbers("paanch, sau")] == [5, 100]  # not 500


def test_compound_units_only_join_tens_and_ones():
    # "bees paanch" = 25 joins; "paanch teen" must not become 8
    assert [n.value for n in extract_numbers("bees paanch")] == [25]
    assert 8 not in [n.value for n in extract_numbers("paanch teen")]
