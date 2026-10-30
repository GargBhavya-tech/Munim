"""Redaction (Bible section 27).

Runs on every inbound text BEFORE anything is stored or shown to a model.

Design:
  * Indic digits (any Unicode decimal digit) are normalised to ASCII first, length-preserving, so every
    span below is an index into both the original and the normalised text.
  * Detectors only COLLECT spans; overlapping spans are merged (union, so no digit of either survives) and the
    text is rebuilt once. A replacement is never re-scanned.
  * Fail-closed: any exception becomes RedactionFailed. Callers use redact_or_drop(): None means "drop the
    message and ask the merchant to repeat". A false redaction only loses text; a leak is a bug.
  * After building the output a verifier re-checks that no secret digit string survived (defence in depth).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

__all__ = ["RedactResult", "Redactor", "RedactionFailed", "redact", "redact_or_drop", "redact_or_repeat",
           "normalise_digits", "PLEASE_REPEAT", "REPEAT_SIGNAL"]

REPEAT_SIGNAL = "please_repeat"
# NATIVE-SPEAKER REVIEW EXPECTED for hi / kn / hinglish wording.
PLEASE_REPEAT = {
    "en": "Sorry, please send your message again.",
    "hi": "माफ़ कीजिए, कृपया अपना संदेश दोबारा भेजिए।",
    "hinglish": "Maaf kijiye, kripya apna message dobara bhejiye.",
    "kn": "ಕ್ಷಮಿಸಿ, ದಯವಿಟ್ಟು ನಿಮ್ಮ ಸಂದೇಶವನ್ನು ಮತ್ತೊಮ್ಮೆ ಕಳುಹಿಸಿ.",
}


class RedactionFailed(Exception):
    """Redaction could not be completed safely. The caller MUST drop the message."""


@dataclass(frozen=True)
class RedactResult:
    text: str
    secret_shared: bool
    kinds: list[str] = field(default_factory=list)


def normalise_digits(s: str) -> str:
    """Map every Unicode decimal digit (Devanagari, Kannada, Arabic-Indic, full-width...) to ASCII 0-9. Length-preserving."""
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


# ------------------------------------------------------------------ vocab
_OTP_WORDS = {"otp", "code", "password", "passcode", "passwd", "pswd", "pasword", "paasword", "paswad", "ओटीपी", "कोड", "पासवर्ड",
              "ಒಟಿಪಿ", "ಓಟಿಪಿ", "ಕೋಡ್", "ಪಾಸ್ವರ್ಡ್", "ಪಾಸ್ವರ್ಡ"}
_PIN_WORDS = {"pin", "mpin", "upipin", "पिन", "ಪಿನ್"}
_ACCOUNT_WORDS = {"account", "acct", "acc", "a/c", "ac", "khata", "khaata", "khate", "ifsc", "खाता", "खाते", "आईएफएससी", "अकाउंट",
                  "ಖಾತೆ", "ಖಾತೆಯ", "ಅಕೌಂಟ್", "ಐಎಫ್ಎಸ್ಸಿ", "ಐಎಫ್‌ಎಸ್‌ಸಿ"}
_AMOUNT_WORDS_RE = re.compile(r"^(?:rupaye|rupaiye|rupay\w*|rupee\w*|rs|inr|रुपये|रुपए|रुपया|रुपयों|रु|ರೂಪಾಯಿ\w*|ರೂ)$")
_AMOUNT_PREFIX_RE = re.compile(r"(?:₹|(?<![a-z])rs\.?|(?<![a-z])inr|रु\.?|ರೂ\.?)\s*$")
_ZW = dict.fromkeys(map(ord, "​‌‍⁠﻿"))
_TRIM = " \t\r\n:;,.!?()[]{}\"'`|/\\-_=*<>"
_WINDOW = 5


def _norm_word(w: str) -> str:
    return unicodedata.normalize("NFC", w.translate(_ZW)).lower()


def _word_hits(token: str, vocab: set[str], suffixes: tuple[str, ...] = ()) -> bool:
    t = _norm_word(token)
    cands = {t, t.strip(_TRIM), t.replace(".", "").strip(_TRIM)}
    for chunk in re.split(r"[\s\d:;,!?()\[\]{}\"'`|\\\-_=*<>.]+", t):
        cands.add(chunk)
    if "/" in t:
        cands.add(t.strip(_TRIM))
    cands.discard("")
    for c in cands:
        if c in vocab:
            return True
        if suffixes and any(c.endswith(s) for s in suffixes):
            return True
    return False


# ------------------------------------------------------------------ helpers
def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class _Span:
    start: int
    end: int
    repl: str
    kind: str
    prio: int
    secret: bool = False
    secrets: tuple[str, ...] = ()  # digit strings that must not survive in the output


_GROUP_RE = re.compile(r"\d+")


def _chains(text: str, gap_chars: str) -> list[list[re.Match]]:
    """Digit groups linked when separated by exactly one char from gap_chars."""
    chains: list[list[re.Match]] = []
    cur: list[re.Match] = []
    for m in _GROUP_RE.finditer(text):
        if cur and m.start() - cur[-1].end() == 1 and text[cur[-1].end()] in gap_chars:
            cur.append(m)
        else:
            if cur:
                chains.append(cur)
            cur = [m]
    if cur:
        chains.append(cur)
    return chains


def _digits(groups: list[re.Match]) -> str:
    return "".join(g.group() for g in groups)


@dataclass
class _Words:
    tokens: list[tuple[int, int, str]]

    @classmethod
    def of(cls, text: str) -> "_Words":
        return cls([(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+", text)])

    def index_at(self, pos: int) -> int:
        # token containing pos, or the last token starting before it
        lo, hi = 0, len(self.tokens) - 1
        ans = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.tokens[mid][0] <= pos:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    def keyword_indices(self, vocab: set[str], suffixes: tuple[str, ...] = ()) -> list[int]:
        return [i for i, (_, _, w) in enumerate(self.tokens) if _word_hits(w, vocab, suffixes)]

    def near(self, a: int, b: int, kws: list[int]) -> int | None:
        """Smallest word distance between [a,b] token range and any keyword index; None if no keywords."""
        best = None
        for k in kws:
            d = 0 if a <= k <= b else (a - k if k < a else k - b)
            if best is None or d < best:
                best = d
        return best


def _amount_marked(text: str, start: int, end: int, words: _Words) -> bool:
    if _AMOUNT_PREFIX_RE.search(text[max(0, start - 8):start].lower()):
        return True
    m = re.match(r"\s*(\S+)", text[end:])
    if m:
        w = _norm_word(m.group(1)).strip(_TRIM)
        if _AMOUNT_WORDS_RE.match(w):
            return True
    return False


# ------------------------------------------------------------------ the redactor
class Redactor:
    """Holds the per-conversation token registry so [PHONE_n] / [NAME_n] numbering is stable across messages.

    In-memory only. Do not pickle or persist a Redactor: the phone registry holds raw numbers to keep tokens stable.
    """

    def __init__(self, names: Iterable[str] | None = None):
        self._names: list[str] = []
        self._phones: dict[str, int] = {}
        for n in names or ():
            self.register_name(n)

    def __repr__(self) -> str:  # never print the registry
        return f"Redactor(names={len(self._names)}, phones={len(self._phones)})"

    def register_name(self, name: str) -> int:
        name = unicodedata.normalize("NFC", name.strip())
        if not name:
            raise ValueError("empty name")
        key = name.casefold()
        for i, n in enumerate(self._names):
            if n.casefold() == key:
                return i + 1
        self._names.append(name)
        return len(self._names)

    def name_token(self, name: str) -> str:
        return f"[NAME_{self.register_name(name)}]"

    def _phone_token(self, digits10: str) -> str:
        n = self._phones.setdefault(digits10, len(self._phones) + 1)
        return f"[PHONE_{n}]"

    # ---- public
    def redact(self, text: str) -> RedactResult:
        try:
            if not isinstance(text, str):
                raise TypeError("text must be str")
            return self._redact(text)
        except RedactionFailed:
            raise
        except Exception as exc:  # fail closed, whatever went wrong
            raise RedactionFailed(f"{type(exc).__name__}: {exc}") from exc

    # ---- internals
    def _redact(self, text: str) -> RedactResult:
        norm = normalise_digits(text)
        spans = _find_spans(self, norm)
        out, kinds, secret = _apply(norm, spans)
        _verify(out, spans, self)
        return RedactResult(out, secret, kinds)


def _apply(norm: str, spans: list[_Span]) -> tuple[str, list[str], bool]:
    """Merge overlapping spans (union: no digit of either survives; the highest-priority replacement wins) and rebuild."""
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    merged: list[_Span] = []
    for s in ordered:
        if merged and s.start < merged[-1].end:
            p = merged[-1]
            win = s if s.prio < p.prio else p
            merged[-1] = _Span(p.start, max(p.end, s.end), win.repl, win.kind, win.prio, p.secret or s.secret, p.secrets + s.secrets)
        else:
            merged.append(_Span(s.start, s.end, s.repl, s.kind, s.prio, s.secret, s.secrets))
    pieces: list[str] = []
    pos = 0
    for s in merged:
        pieces.append(norm[pos:s.start])
        pieces.append(s.repl)
        pos = s.end
    pieces.append(norm[pos:])
    kinds: list[str] = []
    for s in ordered:  # every kind that contributed, in text order, even if it lost a merge
        if s.kind not in kinds:
            kinds.append(s.kind)
    return "".join(pieces), kinds, any(s.secret for s in ordered)


def _verify(out: str, spans: list[_Span], reg: Redactor) -> None:
    """Defence in depth: long secret digit strings must be gone, and a second pass must find nothing secret left."""
    for s in spans:
        for sec in s.secrets:
            if len(sec) >= 6 and sec in out:
                raise RedactionFailed("secret digits survived redaction")
    if re.search(r"\[(?:OTP|PIN)\][ \-,.]?\d|\d[ \-,.]?\[(?:OTP|PIN)\]", out):
        raise RedactionFailed("digits adjacent to a redaction token")
    neutral = re.sub(r"([X…]-?)\d+", r"\1#", out)  # digits kept on purpose by a mask (XXXX-XXXX-1234, XXXXX1234X, XXXX…1234)
    again = _find_spans(Redactor(), neutral)
    if any(s.secret for s in again):
        raise RedactionFailed("secret digits survived redaction (second pass)")


def _find_spans(reg: Redactor, norm: str) -> list[_Span]:
    spans: list[_Span] = []
    words = _Words.of(norm)
    acct_kw = words.keyword_indices(_ACCOUNT_WORDS)

    # ---- PAN
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{5})(\d{4})([A-Za-z])(?![A-Za-z0-9])", norm):
        spans.append(_Span(m.start(), m.end(), "XXXXX" + m.group(2) + "X", "PAN", 5, secrets=(m.group(1).upper(),)))

    # ---- structural: card / aadhaar / bank / phone over chains joined by space or hyphen
    struct: list[_Span] = []
    for chain in _chains(norm, " -"):
        struct.extend(_classify_chain(reg, norm, chain, words, acct_kw))
    spans.extend(struct)

    # ---- OTP / PIN in context
    otp_kw = words.keyword_indices(_OTP_WORDS, ("otp",))
    pin_kw = words.keyword_indices(_PIN_WORDS, ("pin",))
    if otp_kw or pin_kw:
        covered = [(s.start, s.end) for s in spans]
        for chain in _chains(norm, " -,."):
            a, b = chain[0].start(), chain[-1].end()
            if any(cs <= a and b <= ce for cs, ce in covered):
                continue
            ia, ib = words.index_at(a), words.index_at(b - 1)
            do, dp = words.near(ia, ib, otp_kw), words.near(ia, ib, pin_kw)
            cand = [(d, k) for d, k in ((do, "OTP"), (dp, "PIN")) if d is not None and d <= _WINDOW]
            if not cand:
                continue
            kind = min(cand, key=lambda t: (t[0], t[1] != "PIN"))[1]  # nearest keyword; PIN wins a tie
            if _amount_marked(norm, a, b, words):
                continue
            n = len(_digits(chain))
            repl = f"[{kind}]"
            if 4 <= n <= 8:
                spans.append(_Span(a, b, repl, kind, 0, True, (_digits(chain),)))
            elif n > 8:
                for g in chain:
                    if 4 <= len(g.group()) <= 8:
                        spans.append(_Span(g.start(), g.end(), repl, kind, 0, True, (g.group(),)))
                if not any(s.start < b and a < s.end for s in struct):
                    spans.append(_Span(a, b, repl, kind, 9, True, (_digits(chain),)))

    # ---- known names
    for nm in sorted(reg._names, key=len, reverse=True):
        pat = r"(?<![0-9A-Za-zऀ-ॿಀ-೿])" + re.escape(nm) + r"(?![0-9A-Za-zऀ-ॿಀ-೿])"
        for m in re.finditer(pat, norm, flags=re.IGNORECASE):
            spans.append(_Span(m.start(), m.end(), f"[NAME_{reg.register_name(nm)}]", "NAME", 6))
    return spans


def _classify_chain(reg: Redactor, norm: str, chain: list[re.Match], words: _Words, acct_kw: list[int]) -> list[_Span]:
    g = len(chain)
    if g > 40:  # pathological input: whole chain and single groups only
        ranges = [(0, g - 1)] + [(i, i) for i in range(g)]
    else:
        ranges = [(i, j) for width in range(g, 0, -1) for i in range(0, g - width + 1) for j in (i + width - 1,)]
    taken: list[tuple[int, int]] = []
    out: list[_Span] = []
    for i, j in ranges:
        a, b = chain[i].start(), chain[j].end()
        if any(a < te and ts < b for ts, te in taken):
            continue
        d = _digits(chain[i:j + 1])
        n = len(d)
        if n < 9:
            continue
        near_acct = False
        if acct_kw:
            dist = words.near(words.index_at(a), words.index_at(b - 1), acct_kw)
            near_acct = dist is not None and dist <= _WINDOW
        sp = _classify_digits(reg, norm, a, b, d, near_acct)
        if sp is not None:
            taken.append((sp.start, sp.end))
            out.append(sp)
    return out


def _classify_digits(reg: Redactor, norm: str, a: int, b: int, d: str, near_acct: bool) -> _Span | None:
    n = len(d)
    last4 = d[-4:]
    if near_acct and 9 <= n <= 18:
        return _Span(a, b, "X" * (n - 4) + last4, "BANK_ACCOUNT", 1, secrets=(d[:-4],) if n - 4 >= 4 else ())
    if 13 <= n <= 19 and _luhn(d):
        return _Span(a, b, "XXXX…" + last4, "CARD", 2, secrets=(d[:-4],))
    ten = None
    if n == 10 and d[0] in "6789":
        ten = d
    elif n == 11 and d[0] == "0" and d[1] in "6789":
        ten = d[1:]
    elif n == 12 and d[:2] == "91" and d[2] in "6789":
        ten = d[2:]
    if ten is not None:
        start = a - 1 if a > 0 and norm[a - 1] == "+" else a
        return _Span(start, b, reg._phone_token(ten), "PHONE", 4, secrets=(ten,))
    if n == 12:
        return _Span(a, b, "XXXX-XXXX-" + last4, "AADHAAR", 3, secrets=(d[:8],))
    return None


# ------------------------------------------------------------------ module API
def redact(text: str, *, names: Iterable[str] | None = None) -> RedactResult:
    """Stateless redaction (fresh token registry). Raises RedactionFailed on any internal error."""
    return Redactor(names).redact(text)


def redact_or_drop(text: str, *, names: Iterable[str] | None = None, redactor: Redactor | None = None) -> RedactResult | None:
    """Fail-closed wrapper: returns None when redaction fails; the caller drops the message and sends PLEASE_REPEAT."""
    try:
        return (redactor or Redactor(names)).redact(text)
    except RedactionFailed:
        return None


def redact_or_repeat(text: str, *, names: Iterable[str] | None = None, redactor: Redactor | None = None,
                     language: str = "en") -> tuple[RedactResult | None, str | None]:
    """(result, None) on success; (None, please-repeat text in `language`) when the message must be dropped."""
    res = redact_or_drop(text, names=names, redactor=redactor)
    if res is None:
        return None, PLEASE_REPEAT.get(language, PLEASE_REPEAT["en"])
    return res, None
