"""Code-mix mirroring (#137): measure how the merchant writes, hand the renderer a STYLE hint. Code measures; the model only phrases.

``analyse`` counts tokens in the merchant's last N messages: Devanagari -> Hindi, Kannada script -> Kannada, Latin tokens found in a small
romanised-Hindi / romanised-Kannada word list -> that language, every other Latin token -> English. From the counts it derives a ratio
bucket, a script preference and a formality, all deterministic (no model, no clock).

``style_hint`` turns that into the ``style_hint`` string for ``Renderer.render`` / ``GroundingGate.run``. Limits that hold by construction:
  * the hint carries NO digits (nothing for the renderer to copy as a number) and only names languages the merchant has used or the
    requested reply language needs, so mirroring cannot introduce a new language;
  * it states that facts, numbers, dates and fixed phrases are untouched. That sentence is a courtesy: the real guards are the renderer's
    mechanical limits, the faithfulness check and the compliance check, which run on the mirrored reply exactly as on any other;
  * pure English replies (en-IN) get no hint.
``new_language_violations`` is a code check that a reply does not use a script the merchant neither used nor was asked for.

NATIVE-SPEAKER REVIEW EXPECTED for the Hindi and Kannada word lists below (kept to simple, common words).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

DEVANAGARI = re.compile(r"[ऀ-ॿ]+")
KANNADA = re.compile(r"[ಀ-೿]+")
LATIN = re.compile(r"[A-Za-z]+")
TOKEN = re.compile(r"[ऀ-ॿ]+|[ಀ-೿]+|[A-Za-z]+")

HI_ROMAN = frozenset("""hai hain nahi nahin kya mera meri mere aap aapka aapki aapke apna apni kab kyun kyu kaise kaisa mein mujhe humein hum humara
ka ki ke ko se par ho hua hui gaya gayi gaye tha thi the abhi bhejo bhej paisa paise aaya aaye aayi chahiye karo kar kare kijiye karna batao
bataiye kripya dhanyavaad haan theek thik accha achha bahut lekin aur toh yeh ye woh wo tum tumhara yaar bhai naya nayi wala wali raha rahi rahe
hoga hogi hoge milega milegi mila diya liya dena lena kuch koi sab bilkul abhi kal aaj jaldi""".split())
KN_ROMAN = frozenset("""illa alla yenu yaake yaavaga hegide nanna nimma neevu neenu beku bekagide aagide aagilla haudu sari dayavittu namaskara banni
estu hana haNa maadi madi ide ella innu swalpa sahaya aagide aagutte""".split())
FORMAL = frozenset("""aap aapka aapki aapke kripya kijiye bataiye ji dhanyavaad आप आपका आपकी आपके कृपया जी धन्यवाद नमस्ते neevu nimma dayavittu ನೀವು ನಿಮ್ಮ ದಯವಿಟ್ಟು""".split())
INFORMAL = frozenset("""tum tumhara tu tera teri yaar bhai तुम तुम्हारा तू तेरा यार भाई neenu ನೀನು ನಿನ್ನ""".split())


@dataclass(frozen=True)
class MixProfile:
    hi: int                  # Hindi tokens, either script
    kn: int
    en: int
    native_script: int       # of the hi+kn tokens, how many were in Devanagari / Kannada script
    formal: int
    informal: int

    @property
    def native(self) -> int:
        return self.hi + self.kn

    @property
    def total(self) -> int:
        return self.native + self.en

    @property
    def native_share_permille(self) -> int:
        """native / (native + English), integer permille (no floats)."""
        return (1000 * self.native) // self.total if self.total else 0

    @property
    def bucket(self) -> str:
        if not self.total:
            return "unknown"
        p = self.native_share_permille
        return "mostly_english" if p < 150 else "english_leaning" if p < 400 else "balanced" if p < 600 else "native_leaning" if p < 850 else "mostly_native"

    @property
    def native_language(self) -> str | None:
        if not self.native:
            return None
        return "kn" if self.kn > self.hi else "hi"

    @property
    def script(self) -> str:
        """'native' when at least half the Hindi/Kannada tokens were in their own script, else 'roman'."""
        return "native" if self.native and 2 * self.native_script >= self.native else "roman"

    @property
    def formality(self) -> str:
        return "informal" if self.informal > self.formal else "formal" if self.formal > self.informal else "neutral"

    def used_languages(self) -> set[str]:
        return {k for k, n in (("hi", self.hi), ("kn", self.kn), ("en", self.en)) if n}


def analyse(messages: Sequence[str], n: int = 5) -> MixProfile:
    """Measure the last ``n`` messages (oldest first in, the final ``n`` used)."""
    hi = kn = en = nat = formal = informal = 0
    for msg in list(messages)[-n:] if n > 0 else []:
        for tok in TOKEN.findall(msg or ""):
            low = tok.lower()
            if DEVANAGARI.fullmatch(tok):
                hi += 1; nat += 1
            elif KANNADA.fullmatch(tok):
                kn += 1; nat += 1
            elif low in HI_ROMAN:
                hi += 1
            elif low in KN_ROMAN:
                kn += 1
            else:
                en += 1
            formal += tok in FORMAL or low in FORMAL
            informal += tok in INFORMAL or low in INFORMAL
    return MixProfile(hi, kn, en, nat, formal, informal)


_LANG_NAME = {"hi": "Hindi", "kn": "Kannada", "en": "English"}
_REPLY_LANGS = {"hi-IN": {"hi"}, "kn-IN": {"kn"}, "hinglish": {"hi", "en"}, "en-IN": {"en"}}
_BUCKET_TEXT = {"mostly_english": "almost all English with a few Hindi or Kannada words",
                "english_leaning": "mostly English with some {nat}",
                "balanced": "an even mix of {nat} and English",
                "native_leaning": "mostly {nat} with some English words",
                "mostly_native": "almost all {nat} with only a few English words"}


def allowed_languages(profile: MixProfile, reply_language: str) -> set[str]:
    return profile.used_languages() | _REPLY_LANGS.get(reply_language, set())


def style_hint(profile: MixProfile, reply_language: str) -> str | None:
    """The STYLE string for the renderer, or None when there is nothing to mirror (pure English reply or nothing measured)."""
    if reply_language == "en-IN" or profile.bucket == "unknown":
        return None
    nat = _LANG_NAME.get(profile.native_language or "hi") if profile.native else "Hindi"
    parts = [f"Mirror the merchant's mix of Hindi and English: they write {_BUCKET_TEXT[profile.bucket].format(nat=nat)}, in a {profile.formality} register."]
    if reply_language == "hinglish":
        parts.append("Use roman letters." if profile.script == "roman" else "Hindi words may be in Devanagari as the merchant writes them.")
    langs = sorted(_LANG_NAME[k] for k in allowed_languages(profile, reply_language))
    parts.append(f"Use only these languages: {', '.join(langs)}; never add another language.")
    parts.append("Change wording style only: keep every fact, amount, number, date and fixed phrase exactly as given, and do not drop or shorten any fixed phrase.")
    return " ".join(parts)


def new_language_violations(reply_text: str, profile: MixProfile, reply_language: str) -> list[str]:
    """Scripts in the reply that neither the merchant used nor the reply language needs. Empty means fine."""
    ok = allowed_languages(profile, reply_language)
    bad = []
    if DEVANAGARI.search(reply_text) and "hi" not in ok:
        bad.append("hi")
    if KANNADA.search(reply_text) and "kn" not in ok:
        bad.append("kn")
    return bad


def make_style_fn(messages_fn: Callable[[str], Sequence[str]], reply_language_fn: Callable[[str], str], n: int = 5) -> Callable[[str], str | None]:
    """Adapter for the engine's ``style_fn(merchant_id)``: ``messages_fn`` returns the merchant's recent messages (oldest first),
    ``reply_language_fn`` the language this reply will be in."""
    return lambda merchant_id: style_hint(analyse(messages_fn(merchant_id), n), reply_language_fn(merchant_id))
