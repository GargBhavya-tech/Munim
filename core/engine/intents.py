"""Rule-based intent detector (Bible §13, ticket #18): the "disagreement signal" next to the model's intents.

Phrase lists live in the pack (packs/paytm_merchant/intents.yaml) per intent and per language: English,
Hinglish (romanised Hindi), Hindi (Devanagari) and Kannada script. Every list is applied to every message
(people mix languages); ``language_hint`` only breaks ties.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_INTENTS_PATH = Path(__file__).resolve().parents[2] / "packs" / "paytm_merchant" / "intents.yaml"
STRONG, WEAK = 0.8, 0.4
MIN_SCORE = 0.4
_HINT = {"hi": "hi", "kn": "kn", "en": "en", "hinglish": "hinglish", "hi-latn": "hinglish", "hi_latn": "hinglish"}
_INDIC = re.compile(r"[ऀ-ॿಀ-೿]")
_REMOVE = dict.fromkeys([0x200C, 0x200D, 0x093C])      # ZWNJ, ZWJ, Devanagari nukta
_SHORT_STEM = 4                                         # Indic phrases this short may not run into a vowel sign


def normalise_text(text: str) -> str:
    """NFC, lower-case, Indic digits (०-९, ೦-೯, ...) to ASCII, drop ZWJ/ZWNJ/nukta, punctuation to spaces."""
    t = unicodedata.normalize("NFC", text or "").lower()
    out = []
    for ch in t:
        if ord(ch) in _REMOVE:
            continue
        cat = unicodedata.category(ch)
        if cat == "Nd":
            out.append(str(unicodedata.digit(ch)))
        elif cat[0] in "LMN":
            out.append(ch)
        else:
            out.append(" ")
    t = "".join(out)
    t = unicodedata.normalize("NFC", t.translate(_REMOVE))
    return " ".join(t.split())


class _Phrase:
    __slots__ = ("text", "lang", "weight", "indic", "rx")

    def __init__(self, text: str, lang: str, weight: float):
        self.text, self.lang, self.weight = normalise_text(text), lang, weight
        self.indic = bool(_INDIC.search(self.text))
        self.rx = None if self.indic else re.compile(r"(?<![^\W_])" + re.escape(self.text) + r"(?![^\W_])")

    def matches(self, norm: str) -> bool:
        if not self.indic:
            return self.rx.search(norm) is not None
        start = 0
        while True:
            i = norm.find(self.text, start)
            if i < 0:
                return False
            end = i + len(self.text)
            if len(self.text) > _SHORT_STEM or end >= len(norm) or not unicodedata.category(norm[end]).startswith("M"):
                return True
            start = i + 1


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[list[str], dict[str, list[_Phrase]], dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["intents"]
    order, phrases, mapping = [], {}, {}
    for name, spec in data.items():
        order.append(name)
        mapping[name] = spec["playbook"]
        ps: dict[str, _Phrase] = {}      # the same phrase listed under two languages counts once (strong wins)
        for kind, w in (("strong", STRONG), ("weak", WEAK)):
            for lang, items in (spec.get(kind) or {}).items():
                for raw in items:
                    ph = _Phrase(str(raw), lang, w)
                    if ph.text not in ps:
                        ps[ph.text] = ph
        phrases[name] = list(ps.values())
    return order, phrases, mapping


def intent_to_playbook(path: str | Path = DEFAULT_INTENTS_PATH) -> dict[str, str]:
    """Intent -> playbook id for the pack. Intents not in the map are unsupported (X0_UNSUPPORTED)."""
    return dict(_load(str(path))[2])


INTENT_TO_PLAYBOOK: dict[str, str] = intent_to_playbook()
CONTROL_INTENTS = frozenset(i for i, p in INTENT_TO_PLAYBOOK.items() if p.startswith("CTRL_"))
UNSUPPORTED_PLAYBOOK = "X0_UNSUPPORTED"


def detect_intent(text: str, language_hint: str | None = None, path: str | Path = DEFAULT_INTENTS_PATH) -> list[tuple[str, float]]:
    """All intents whose phrases match, best first: [(intent, score), ...]. Empty list = no rule fired.

    Score is noisy-or over matched phrases (strong 0.8, weak 0.4), so two weak hints beat one. Ties go to the
    intent with the longest matched phrase, then to the pack's intent order."""
    order, phrases, _ = _load(str(path))
    norm = normalise_text(text)
    hint = _HINT.get((language_hint or "").lower())
    found = []
    for idx, name in enumerate(order):
        miss, longest = 1.0, 0
        hit = False
        for p in phrases[name]:
            if p.text and p.matches(norm):
                w = p.weight + (0.02 if hint and p.lang == hint else 0.0)
                miss *= 1.0 - w
                longest = max(longest, len(p.text))
                hit = True
        if hit:
            score = min(0.97, 1.0 - miss)
            if score >= MIN_SCORE:
                found.append((name, round(score, 3), longest, idx))
    found.sort(key=lambda r: (-r[1], -r[2], r[3]))
    return [(n, s) for n, s, _, _ in found]


def top_intent(text: str, language_hint: str | None = None) -> str | None:
    r = detect_intent(text, language_hint)
    return r[0][0] if r else None


_LANG_NAMES = {
    "kn": ["kannada", "ಕನ್ನಡ", "कन्नड"],
    "hi": ["hindi", "हिंदी", "हिन्दी", "ಹಿಂದಿ"],
    "en": ["english", "अंग्रेजी", "इंग्लिश", "ಇಂಗ್ಲಿಷ್"],
}


def language_switch_target(text: str) -> str | None:
    """For a SWITCH_LANGUAGE message: 'kn' | 'hi' | 'en' (the language asked for), else None."""
    norm = normalise_text(text)
    best, pos = None, -1
    for code, names in _LANG_NAMES.items():
        for n in names:
            i = norm.find(normalise_text(n))
            if i >= 0 and (best is None or i < pos):
                best, pos = code, i
    return best
