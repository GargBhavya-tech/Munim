"""Number normalisation for the grounding gate (Bible section 29).

normalise_digits: Indic digits -> ASCII (length preserving, so spans index the original text too).
extract_numbers: every number / amount / date / time in a text, normalised.
  money -> value is integer PAISE; count/plain -> int (Decimal if fractional); date -> ISO string; time -> 24h "HH:MM".
Forms: "Rs 2,000", "2000", "2k", "do hazaar", "दो हज़ार", "ಎರಡು ಸಾವಿರ", "1.5 lakh", Indian grouping "1,50,000".

Over-extraction is the safe direction for the gate (a spurious number in a claim only makes the claim fail).
NATIVE-SPEAKER REVIEW EXPECTED for the Hindi / Kannada number-word tables. Kannada compounds written with sandhi
(for example 25, 99) are NOT in the table; digits are the normal form for those.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

__all__ = ["Num", "normalise_digits", "extract_numbers", "to_paise", "number_key"]


def normalise_digits(s: str) -> str:
    """Map every Unicode decimal digit (Devanagari, Kannada, ...) to ASCII 0-9. Length-preserving."""
    out = []
    for ch in s:
        if ch.isascii():
            out.append(ch)
        else:
            try:
                out.append(str(unicodedata.decimal(ch)))
            except ValueError:
                out.append(ch)
    return "".join(out)


@dataclass(frozen=True)
class Num:
    kind: str                    # money | count | date | time | plain
    value: int | str | Decimal   # money: int paise; count: int; plain: int (Decimal if fractional); date: ISO; time: "HH:MM"
    span: tuple[int, int]        # (start, end) in the input text
    raw: str

    @property
    def units(self) -> Decimal | None:
        """Magnitude in rupees / plain units for numeric kinds, None for date and time."""
        if self.kind == "money":
            return Decimal(self.value) / 100
        if self.kind in ("count", "plain"):
            return Decimal(self.value)
        return None


def number_key(n: Num) -> tuple[str, object]:
    """Comparison key: numeric kinds compare by magnitude (so Rs 8,200 == 8200.00 == 8200); date/time by their ISO text."""
    if n.kind in ("date", "time"):
        return (n.kind, n.value)
    u = n.units
    return ("num", u.normalize() if u != 0 else Decimal(0))


def to_paise(amount) -> int:
    """Rupees (int / str / Decimal / float) -> integer paise, rounding half up. 8200 -> 820000, '8200.50' -> 820050."""
    if isinstance(amount, bool):
        raise TypeError("bool is not an amount")
    d = Decimal(str(amount)) if not isinstance(amount, Decimal) else amount
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------ word tables
def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s).lower()


_HI_DEV = (
    "एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस "
    "इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस "
    "इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस "
    "इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास "
    "इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर "
    "इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी "
    "इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे "
    "इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(_HI_DEV) == 99

_WORDS: dict[str, tuple[str, Fraction]] = {}   # token -> (class, value); class in unit|mult|frac


def _add(words: str, cls: str, val) -> None:
    for w in words.split():
        _WORDS[_nfc(w)] = (cls, Fraction(val))


for _i, _w in enumerate(_HI_DEV, start=1):
    _add(_w, "unit", _i)
for _w, _v in (("पांच", 5), ("छः", 6), ("छे", 6), ("पन्द्रह", 15), ("अठाईस", 28), ("हजार", 1000)):
    _add(_w, "unit" if _v < 100 else "mult", _v)

# Romanised Hindi / Hinglish: 1-20 and the tens (compounds in Roman are too spelling-variable; use digits)
_ROMAN_HI = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "paach": 5, "chhe": 6, "chhah": 6, "cheh": 6,
    "che": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "dus": 10, "gyarah": 11, "barah": 12, "terah": 13,
    "chaudah": 14, "chaudaha": 14, "pandrah": 15, "pandra": 15, "solah": 16, "satrah": 17, "atharah": 18, "athaarah": 18, "unnis": 19,
    "unees": 19, "bees": 20, "tees": 30, "chalis": 40, "chaalis": 40, "pachas": 50, "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90,
    "pachees": 25, "pacchis": 25, "pachis": 25,
}
for _w, _v in _ROMAN_HI.items():
    _add(_w, "unit", _v)

_EN = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
       "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
       "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
for _w, _v in _EN.items():
    _add(_w, "unit", _v)

_KN = {"ಒಂದು": 1, "ಎರಡು": 2, "ಮೂರು": 3, "ನಾಲ್ಕು": 4, "ಐದು": 5, "ಆರು": 6, "ಏಳು": 7, "ಎಂಟು": 8, "ಒಂಬತ್ತು": 9, "ಹತ್ತು": 10,
       "ಹನ್ನೊಂದು": 11, "ಹನ್ನೆರಡು": 12, "ಹದಿಮೂರು": 13, "ಹದಿನಾಲ್ಕು": 14, "ಹದಿನೈದು": 15, "ಹದಿನಾರು": 16, "ಹದಿನೇಳು": 17,
       "ಹದಿನೆಂಟು": 18, "ಹತ್ತೊಂಬತ್ತು": 19, "ಇಪ್ಪತ್ತು": 20, "ಮೂವತ್ತು": 30, "ನಲವತ್ತು": 40, "ಐವತ್ತು": 50, "ಅರವತ್ತು": 60,
       "ಎಪ್ಪತ್ತು": 70, "ಎಂಬತ್ತು": 80, "ತೊಂಬತ್ತು": 90}
for _w, _v in _KN.items():
    _add(_w, "unit", _v)

_add("sau सौ ನೂರು hundred", "mult", 100)
_add("hazaar hazar hajar hajaar हज़ार हजार ಸಾವಿರ thousand", "mult", 1000)
_add("lakh lakhs lac lacs लाख ಲಕ್ಷ", "mult", 100_000)
_add("crore crores karod karor करोड़ करोड ಕೋಟಿ", "mult", 10_000_000)
_add("million", "mult", 1_000_000)
_add("dedh डेढ़ डेढ", "frac", Fraction(3, 2))
_add("dhai ढाई अढ़ाई", "frac", Fraction(5, 2))

# a lone one of these is far more often an ordinary word ("bhej do", "ek minute", "one moment") than a number
_AMBIGUOUS_ALONE = {_nfc(w) for w in ("do", "ek", "दो", "एक", "ಒಂದು", "teen", "sat", "ath", "das", "dus", "che")}
_AMBIGUOUS_ALONE_EN = {"one"}

_KN_CASE_SUFFIXES = ("ಗಳು", "ದ", "ಕ್ಕೆ", "ವನ್ನು", "ಗಳ")


def _lookup(tok: str):
    t = _nfc(tok)
    hit = _WORDS.get(t)
    if hit:
        return hit
    if "ಀ" <= t[:1] <= "೿":
        for suf in _KN_CASE_SUFFIXES:
            if t.endswith(suf) and len(t) > len(suf):
                hit = _WORDS.get(t[: -len(suf)])
                if hit:
                    return hit
    return None


# ------------------------------------------------------------------ regexes
_WCH = "0-9A-Za-z\u0900-\u0963\u0966-\u097F\u0C80-\u0CFF"  # Devanagari minus danda
_TOKEN_RE = re.compile(f"[{_WCH}]+")
_DIGITS = r"(?:\d{1,2}(?:,\d{2})+,\d{3}|\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_DIGITS_RE = re.compile(rf"(?<!\d){_DIGITS}")
_PREFIX_MONEY = re.compile(r"(?:₹|(?<![a-z])rs\.?|(?<![a-z])inr|(?<![a-z])rupees?|(?<![a-z])rupaye|रु\.?|रुपये|ರೂ\.?)\s*$")
_SUFFIX_MONEY = re.compile(r"\s*(?:rupaye|rupaiye|rupay\w*|rupee\w*|rs\b|inr\b|₹|रुपये|रुपए|रुपया|रुपयों|रु\b|ರೂಪಾಯಿ\w*|ರೂ\b|/-)", re.I)
_COUNT_NOUNS = {_nfc(w) for w in (
    "day days din dinon hour hours ghante ghanta minute minutes month months mahine mahina mahinon week weeks hafte year years saal "
    "time times baar attempt attempts document documents docs order orders transaction transactions item items instalment instalments "
    "emi emis step steps payment payments customer customers percent % दिन घंटे घंटा महीने महीना हफ्ते साल बार दस्तावेज़ दस्तावेज "
    "ದಿನ ದಿನಗಳು ಗಂಟೆ ತಿಂಗಳು ವಾರ ವರ್ಷ ಬಾರಿ ದಾಖಲೆ ದಾಖಲೆಗಳು"
).split()}

_MONTHS = {m: i for i, names in enumerate(
    ["jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july", "aug august", "sep sept september",
     "oct october", "nov november", "dec december"], start=1) for m in names.split()}
_MON_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_RES = [
    (re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})(?!\d)"), "dmy"),
    (re.compile(rf"(?<![0-9a-z])(\d{{1,2}})(?:st|nd|rd|th)?[ \-]+({_MON_RE})\.?,?(?:[ \-]+(\d{{4}}))?(?![0-9a-z])", re.I), "d_mon_y"),
    (re.compile(rf"(?<![0-9a-z])({_MON_RE})\.?[ \-]+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?[ \-]+(\d{{4}}))?(?![0-9a-z])", re.I), "mon_d_y"),
]
_TIME_RE = re.compile(r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2})(?::\d{2})?)?\s*([ap])\.?m\.?(?![a-z])|(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)", re.I)


def _iso(y, m, d):
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    if y is None:
        return f"--{m:02d}-{d:02d}"
    y = int(y)
    if y < 100:
        y += 2000
    return f"{y:04d}-{m:02d}-{d:02d}"


def _overlaps(a: int, b: int, taken: list[tuple[int, int]]) -> bool:
    return any(a < e and s < b for s, e in taken)


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _int_or_dec(d: Decimal):
    return int(d) if d == d.to_integral_value() else d


def _paise(d: Decimal) -> int:
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def extract_numbers(text: str, *, english_one: bool = False) -> list[Num]:
    """All numbers in `text`, in text order. `english_one=True` also reads a lone English "one" as 1 (used for English claims)."""
    t = normalise_digits(text)
    low = t.lower()
    taken: list[tuple[int, int]] = []
    found: list[Num] = []

    # ---- dates
    for rx, form in _DATE_RES:
        for m in rx.finditer(low):
            if _overlaps(m.start(), m.end(), taken):
                continue
            g = m.groups()
            if form == "ymd":
                iso = _iso(g[0], int(g[1]), int(g[2]))
            elif form == "dmy":
                iso = _iso(g[2], int(g[1]), int(g[0]))
            elif form == "d_mon_y":
                iso = _iso(g[2], _MONTHS[g[1].lower()], int(g[0]))
            else:
                iso = _iso(g[2], _MONTHS[g[0].lower()], int(g[1]))
            if iso:
                taken.append((m.start(), m.end()))
                found.append(Num("date", iso, (m.start(), m.end()), text[m.start():m.end()]))

    # ---- times
    for m in _TIME_RE.finditer(low):
        if _overlaps(m.start(), m.end(), taken):
            continue
        if m.group(3):
            h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
            if not (1 <= h <= 12):
                continue
            h = h % 12 + (12 if ap == "p" else 0)
        else:
            h, mi = int(m.group(4)), int(m.group(5))
        taken.append((m.start(), m.end()))
        found.append(Num("time", f"{h:02d}:{mi:02d}", (m.start(), m.end()), text[m.start():m.end()]))

    # ---- digit numbers (with optional multiplier word / k)
    for m in _DIGITS_RE.finditer(low):
        if _overlaps(m.start(), m.end(), taken):
            continue
        val = _dec(m.group())
        end = m.end()
        nxt = re.compile(f"(\\s*)([{_WCH}]+)").match(low, end)
        if nxt:
            sp, tok = nxt.group(1), nxt.group(2)
            hit = _lookup(tok)
            if hit and hit[0] == "mult":
                val *= Decimal(hit[1].numerator) / Decimal(hit[1].denominator)
                end = nxt.end()
            elif tok == "k" and not sp:
                val *= 1000
                end = nxt.end()
            elif tok == "cr":
                val *= 10_000_000
                end = nxt.end()
        found.append(_classify(text, low, m.start(), end, val))
        taken.append((m.start(), end))

    # ---- number words
    toks = [(m.start(), m.end(), m.group()) for m in _TOKEN_RE.finditer(low) if not _overlaps(m.start(), m.end(), taken)]
    i = 0
    while i < len(toks):
        hit = _lookup(toks[i][2])
        if not hit:
            i += 1
            continue
        j, total, cur, has_mult, prev_unit = i, Fraction(0), Fraction(0), False, None
        count_tokens = 0
        while j < len(toks):
            if j > i and low[toks[j - 1][1]:toks[j][0]].strip() != "":
                break
            h = _lookup(toks[j][2])
            if not h:
                break
            cls, v = h
            if cls == "unit":
                if prev_unit is not None and not (prev_unit >= 20 and prev_unit % 10 == 0 and v < 10):
                    break
                cur += v
                prev_unit = int(v)
            elif cls == "frac":
                if cur != 0 or prev_unit is not None:
                    break
                cur = v
                prev_unit = None
            else:
                if v == 100:
                    cur = (cur or 1) * 100
                else:
                    total += (cur or 1) * v
                    cur = Fraction(0)
                has_mult = True
                prev_unit = None
            count_tokens += 1
            j += 1
        if j == i:
            i += 1
            continue
        value = total + cur
        first = _nfc(toks[i][2])
        if not has_mult and j - i == 1 and (first in _AMBIGUOUS_ALONE or (first in _AMBIGUOUS_ALONE_EN and not english_one)):
            i = j
            continue
        if value.denominator != 1:
            dval = Decimal(value.numerator) / Decimal(value.denominator)
        else:
            dval = Decimal(value.numerator)
        found.append(_classify(text, low, toks[i][0], toks[j - 1][1], dval))
        i = j

    found.sort(key=lambda n: n.span)
    return found


def _classify(text: str, low: str, start: int, end: int, val: Decimal) -> Num:
    raw = text[start:end]
    before, after = low[max(0, start - 12):start], low[end:end + 24]
    if _PREFIX_MONEY.search(before) or _SUFFIX_MONEY.match(after):
        return Num("money", _paise(val), (start, end), raw)
    m = re.match(f"\\s*([{_WCH}%]+)", after)
    if m and _nfc(m.group(1)) in _COUNT_NOUNS and val == val.to_integral_value():
        return Num("count", int(val), (start, end), raw)
    return Num("plain", _int_or_dec(val), (start, end), raw)
