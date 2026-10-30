"""Live language switching (#41), by text and by voice.

* An EXPLICIT request ("Hindi mein bolo", "ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", "English please") saves the preference: ``save_preference`` (R1, so it is
  undoable) through the executor, and a ``language`` profile fact that SUPERSEDES the old one (history is kept, nothing is overwritten).
* The saved preference decides the reply language. It is never inferred from the merchant's name, city or segment: no function here
  takes a name.
* When speech recognition hears a different language than the saved one, the preference does not change on its own:
    - low confidence  -> ask once ("Shall we speak in X?"); asking is remembered, so the same flip is never asked twice;
    - high confidence -> reply in that language for THIS turn only (``ADOPT_TURN``); the saved preference stays.
  A later yes saves it; a no is remembered.
Fixed phrases are conversational templates. NATIVE-SPEAKER REVIEW EXPECTED for hi, kn and hinglish.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

from core.actions.types import ActionIntent
from core.engine.intents import language_switch_target, top_intent
from core.memory.profile import ProfileStore
from core.voice.pipeline import load_voice_config
from core.voice.readback import NO, YES

DEFAULT_LANGUAGE = "hi-IN"
_CODES = {"hi": "hi-IN", "kn": "kn-IN", "en": "en-IN"}
NAMES = {"hi-IN": {"en-IN": "Hindi", "hi-IN": "हिंदी", "hinglish": "Hindi", "kn-IN": "ಹಿಂದಿ"},
         "kn-IN": {"en-IN": "Kannada", "hi-IN": "कन्नड़", "hinglish": "Kannada", "kn-IN": "ಕನ್ನಡ"},
         "en-IN": {"en-IN": "English", "hi-IN": "अंग्रेज़ी", "hinglish": "English", "kn-IN": "ಇಂಗ್ಲಿಷ್"},
         "hinglish": {"en-IN": "Hinglish", "hi-IN": "हिंग्लिश", "hinglish": "Hinglish", "kn-IN": "ಹಿಂಗ್ಲಿಷ್"}}
ASK = {"en-IN": "Would you like to speak in {lang}? Please say yes or no.",
       "hi-IN": "क्या आप {lang} में बात करना चाहेंगे? कृपया हाँ या नहीं बोलिए।",
       "hinglish": "Kya aap {lang} mein baat karna chahenge? Kripya haan ya nahi boliye.",
       "kn-IN": "ನೀವು {lang} ನಲ್ಲಿ ಮಾತನಾಡಲು ಬಯಸುವಿರಾ? ದಯವಿಟ್ಟು ಹೌದು ಅಥವಾ ಇಲ್ಲ ಎಂದು ಹೇಳಿ."}


@dataclass(frozen=True)
class SwitchResult:
    changed: bool
    language: str
    reason: str | None = None            # NOT_A_SWITCH | SAME | REFUSED


@dataclass(frozen=True)
class SttLanguage:
    action: str                          # SAME | CONFIRM | ADOPT_TURN | ALREADY_ASKED
    reply_language: str                  # what to reply in this turn
    prompt: str | None = None            # the one question, for CONFIRM


def normalise_code(lang: str | None) -> str | None:
    if not lang:
        return None
    return {"hi": "hi-IN", "hi-in": "hi-IN", "kn": "kn-IN", "kn-in": "kn-IN", "en": "en-IN", "en-in": "en-IN", "hinglish": "hinglish"}.get(lang.strip().lower())


class LanguageManager:
    def __init__(self, conn, clock, profile: ProfileStore, executor=None, *, config: dict | None = None):
        self.conn, self.clock, self.profile, self.executor = conn, clock, profile, executor
        self.flip_confidence = float((config if config is not None else load_voice_config())["language_flip_confidence"])

    # -- reading ------------------------------------------------------------------------------------------------------
    def current(self, merchant_id: str, case_language: str | None = None) -> str:
        """Saved preference, else the case's language, else the default. No name or segment is consulted."""
        fact = self.profile.get(merchant_id, "language")
        if fact is not None and fact.value in NAMES:
            return fact.value
        return normalise_code(case_language) or DEFAULT_LANGUAGE

    # -- explicit request ---------------------------------------------------------------------------------------------
    def apply_explicit_switch(self, merchant_id: str, text: str, *, case_id: str | None, event_id: str, current: str | None = None) -> SwitchResult:
        cur = current or self.current(merchant_id)
        if top_intent(text) != "SWITCH_LANGUAGE":
            return SwitchResult(False, cur, "NOT_A_SWITCH")
        target = _CODES.get(language_switch_target(text) or "")
        if target is None:
            return SwitchResult(False, cur, "NOT_A_SWITCH")
        if target == cur:
            return SwitchResult(False, cur, "SAME")
        return self._save(merchant_id, target, case_id=case_id, event_id=event_id, cur=cur)

    def _save(self, merchant_id: str, target: str, *, case_id: str | None, event_id: str, cur: str) -> SwitchResult:
        if self.executor is not None:
            intent = ActionIntent("save_preference", {"merchant_id": merchant_id, "key": "language", "value": target}, case_id=case_id,
                                  step_seq=zlib.crc32(event_id.encode()) % 1_000_000, allowlist=("save_preference",), channel=None)
            out = self.executor.submit(intent)
            if out.status not in ("OK", "REPLAYED"):
                return SwitchResult(False, cur, "REFUSED")
        self.profile.set_fact(merchant_id, "language", target, "MERCHANT_SAID", evidence_ref=event_id)
        return SwitchResult(True, target)

    # -- speech recognition heard another language -------------------------------------------------------------------------
    def observe_stt(self, merchant_id: str, stt_language: str | None, confidence: float | None, *, case_language: str | None = None) -> SttLanguage:
        cur = self.current(merchant_id, case_language)
        heard = normalise_code(stt_language)
        if heard is None or heard == cur or (cur == "hinglish" and heard in ("hi-IN", "en-IN")):
            return SttLanguage("SAME", cur)
        if confidence is not None and confidence >= self.flip_confidence:
            return SttLanguage("ADOPT_TURN", heard)
        marker = f"langflip_asked:{merchant_id}:{heard}"
        if self.conn.execute("SELECT 1 FROM meta WHERE key = ?", (marker,)).fetchone():
            return SttLanguage("ALREADY_ASKED", cur)
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, 'asked')", (marker,))
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (f"langflip_pending:{merchant_id}", heard))
        return SttLanguage("CONFIRM", cur, ASK.get(cur, ASK["en-IN"]).format(lang=NAMES[heard].get(cur, NAMES[heard]["en-IN"])))

    def answer_flip(self, merchant_id: str, text: str, *, case_id: str | None, event_id: str) -> SwitchResult:
        cur = self.current(merchant_id)
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (f"langflip_pending:{merchant_id}",)).fetchone()
        if row is None:
            return SwitchResult(False, cur, "NOT_A_SWITCH")
        words = [w.strip(".,!?।") for w in text.lower().split()]
        yes, no = any(w in YES for w in words), any(w in NO for w in words)
        if yes == no:
            return SwitchResult(False, cur, "NOT_A_SWITCH")          # unclear: keep waiting, never switch on a guess
        self.conn.execute("DELETE FROM meta WHERE key = ?", (f"langflip_pending:{merchant_id}",))
        if no:
            return SwitchResult(False, cur, "SAME")
        return self._save(merchant_id, row["value"], case_id=case_id, event_id=event_id, cur=cur)
