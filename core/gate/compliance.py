"""Compliance phrases (#49): identity and "Paytm never asks for your PIN or OTP" on first contact, a truthful yes when asked "are you an AI?",
and the reply in the merchant's current language, in every supported language.

These lines are fixed approved text (packs/paytm_merchant/phrases.yaml) added by CODE, so the renderer cannot omit, soften or reword them,
and the faithfulness check treats them as approved phrases. Whether the REPLY LANGUAGE matches the preference is checked on the script of
the text itself (Kannada script for kn-IN, Devanagari for hi-IN, Latin letters only for English and Hinglish).
Question patterns below need native-speaker review.
"""
from __future__ import annotations

import re

from core.engine.intents import normalise_text
from core.gate.phrases import Phrases, canon
from core.gate.render import RenderedSentence

_AI_WORDS = r"(ai|a i|bot|robot|machine|computer|human|insaan|insan|real person|real|person|aadmi|manushya)"
_AI_PATTERNS = [
    re.compile(rf"\bare you (an? )?{_AI_WORDS}\b"), re.compile(rf"\bis this (an? )?{_AI_WORDS}\b"),
    re.compile(rf"\bam i (talking|speaking|chatting) (to|with) (an? )?{_AI_WORDS}\b"), re.compile(r"\byou (a )?(real )?(human|person|bot|robot)\b"),
    re.compile(rf"\bkya (aap|tum|ye|yeh|yah) {_AI_WORDS}( ho| hain| hai)?\b"), re.compile(rf"\b(aap|tum) {_AI_WORDS}( ho| hain)\b"),
    re.compile(r"\b(ye|yeh) (bot|robot|machine|ai) (hai|he)\b"), re.compile(r"\bbot se baat\b"),
]
_AI_INDIC = ["एआई", "रोबोट", "बॉट", "इंसान", "इन्सान", "मशीन", "आदमी हैं", "ಎಐ", "ರೋಬೋಟ್", "ಬಾಟ್", "ಮನುಷ್ಯ", "ಯಂತ್ರ", "ಮನುಷ್ಯರಾ"]
_INDIC_Q = ["क्या आप", "क्या यह", "क्या ये", "आप कौन", "ನೀವು", "ಇದು", "ಯಾರು"]
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_KANNADA = re.compile(r"[ಀ-೿]")


def is_ai_question(text: str) -> bool:
    """True when the merchant is asking whether they are talking to an AI / a bot / a person."""
    t = normalise_text(text)
    if any(p.search(t) for p in _AI_PATTERNS):
        return True
    return any(w in t for w in _AI_INDIC) and any(q in t for q in _INDIC_Q)


def language_ok(text: str, language: str) -> bool:
    """The reply is written in the language the merchant prefers, judged by script."""
    dev, kan = bool(_DEVANAGARI.search(text)), bool(_KANNADA.search(text))
    if language == "kn-IN":
        return kan and not dev
    if language == "hi-IN":
        return dev and not kan
    return not dev and not kan                       # English and Hinglish are written in Latin letters


class ComplianceChecker:
    def __init__(self, phrases: Phrases | None = None):
        self.phrases = phrases or Phrases()

    def required(self, *, first_contact: bool, asked_ai: bool) -> list[str]:
        ids = []
        if asked_ai:
            ids.append("ai_yes")
        if first_contact:
            ids += ["identity", "pin_otp"]
        return ids

    def apply(self, sentences: list[RenderedSentence], language: str, *, first_contact: bool, asked_ai: bool) -> list[RenderedSentence]:
        """Fixed compliance lines first, then the reply. Nothing is duplicated if the reply already carries a line."""
        have = {canon(s.text) for s in sentences}
        lead = [RenderedSentence("compliance", self.phrases.text(pid, language), (), pid)
                for pid in self.required(first_contact=first_contact, asked_ai=asked_ai) if canon(self.phrases.text(pid, language)) not in have]
        return lead + list(sentences)

    def missing(self, sentences: list[RenderedSentence], language: str, *, first_contact: bool, asked_ai: bool) -> list[str]:
        blob = canon(" ".join(s.text for s in sentences))
        return [pid for pid in self.required(first_contact=first_contact, asked_ai=asked_ai) if canon(self.phrases.text(pid, language)) not in blob]
