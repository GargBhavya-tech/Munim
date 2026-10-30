"""Voice, push-to-talk (#39): audio in -> saaras STT -> redaction -> a MERCHANT_MESSAGE event; reply text -> bulbul TTS -> audio out.

Rules that make this safe:
  * STT text is DATA. It becomes an event carrying the REDACTED text; nothing here calls an action. Whatever acts on it goes through the
    engine, guard and executor like a typed message.
  * The confidence field is never ignored. The Sarvam REST response has no per-transcript confidence, only ``language_probability`` (and only
    when the language is auto-detected). A missing value counts as LOW, so ``low_confidence`` is true and #40 requires a read-back before any
    write action. The event carries ``stt_confidence`` and ``low_confidence`` for exactly that.
  * Redaction is fail-closed (#35): if it fails, or the transcript is empty, no event is written and the merchant is asked to repeat.
  * Raw audio is not stored. It is handed to ``audio_sink`` only when the caller says the merchant consented to storage.
  * Per-stage latency (STT, redaction, TTS) is measured and returned; it is also ledgered when a ledger is given, without any transcript text.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from core.engine.events import append_event
from core.engine.intents import top_intent
from core.eval.latency import Stopwatch
from core.llm.sarvam_http import SarvamError
from core.memory.redact import PLEASE_REPEAT, Redactor, redact_or_repeat

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "voice.yaml"
_CODE_TO_LANG = {"hi-IN": "hi-IN", "kn-IN": "kn-IN", "en-IN": "en-IN", "hi": "hi-IN", "kn": "kn-IN", "en": "en-IN"}
_SHORT = {"hi-IN": "hi", "kn-IN": "kn", "en-IN": "en", "hinglish": "hinglish"}
_TTS_CODE = {"hi-IN": "hi-IN", "kn-IN": "kn-IN", "en-IN": "en-IN", "hinglish": "hi-IN"}      # Hinglish is spoken by the Hindi voice


def load_voice_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def short_language(lang: str | None) -> str:
    return _SHORT.get(lang or "", "en")


@dataclass
class VoiceResult:
    dropped: bool
    reason: str | None = None                 # EMPTY_TRANSCRIPT | REDACTION_FAILED | STT_UNAVAILABLE
    repeat_prompt: str | None = None          # what to say back when the turn was dropped
    text: str | None = None                   # the redacted transcript
    language: str | None = None
    confidence: float | None = None
    low_confidence: bool = True
    secret_shared: bool = False
    event_id: str | None = None
    inserted: bool = False                    # False when the same clip was sent twice
    intent: str | None = None
    latency_ms: dict = field(default_factory=dict)


class VoiceTurnProcessor:
    def __init__(self, sarvam: Any, conn, clock, *, config: dict | None = None, ledger=None, redactor_factory: Callable[[], Redactor] = Redactor,
                 audio_sink: Callable[[str, bytes], None] | None = None):
        self.sarvam, self.conn, self.clock, self.ledger = sarvam, conn, clock, ledger
        self.cfg = config if config is not None else load_voice_config()
        self.redactor_factory, self.audio_sink = redactor_factory, audio_sink

    @property
    def min_confidence(self) -> float:
        return float(self.cfg["min_stt_confidence"])

    def is_low(self, confidence: float | None) -> bool:
        return confidence is None or confidence < self.min_confidence

    # -- inbound ------------------------------------------------------------------------------------------------------
    def process(self, wav: bytes, merchant_id: str, *, language_code: str = "unknown", hinglish: bool = False, consent_store_audio: bool = False,
                case_id: str | None = None) -> VoiceResult:
        lat: dict[str, float] = {}
        sw = Stopwatch()
        try:
            res = self.sarvam.stt(wav, language_code, mode="translit" if hinglish else None)
        except (SarvamError, OSError, ValueError):
            lat["stt_ms"] = sw.elapsed_ms()
            return VoiceResult(True, "STT_UNAVAILABLE", PLEASE_REPEAT["en"], latency_ms=lat)
        lat["stt_ms"] = sw.elapsed_ms()
        raw = (res.get("transcript") or "").strip()
        detected = _CODE_TO_LANG.get(res.get("language_code") or "", None)
        lang = "hinglish" if hinglish else (detected or "hi-IN")
        prob = res.get("language_probability")
        confidence = float(prob) if isinstance(prob, (int, float)) and not isinstance(prob, bool) else None
        if not raw:
            return VoiceResult(True, "EMPTY_TRANSCRIPT", PLEASE_REPEAT.get(short_language(lang), PLEASE_REPEAT["en"]), language=lang, confidence=confidence,
                               low_confidence=True, latency_ms=lat)

        sw = Stopwatch()
        red, repeat = redact_or_repeat(raw, redactor=self.redactor_factory(), language=short_language(lang))
        lat["redact_ms"] = sw.elapsed_ms()
        if red is None:
            return VoiceResult(True, "REDACTION_FAILED", repeat, language=lang, confidence=confidence, low_confidence=True, latency_ms=lat)

        event_id = "VOICE-" + hashlib.sha256(f"{merchant_id}|{hashlib.sha256(wav).hexdigest()}".encode()).hexdigest()[:16]
        low = self.is_low(confidence)
        intent = top_intent(red.text, short_language(lang))
        payload = {"text": red.text, "language": lang, "channel": "voice", "stt_confidence": confidence, "low_confidence": low,
                   "secret_shared": red.secret_shared, "intent": intent}
        ins = append_event(self.conn, event_id=event_id, type="MERCHANT_MESSAGE", payload=payload, ts_sim=self.clock.now_iso(),
                           merchant_id=merchant_id, case_id=case_id)
        if consent_store_audio and self.audio_sink is not None:
            self.audio_sink(event_id, wav)
        if self.ledger is not None:
            self.ledger.append(kind="CONTEXT_HASH", actor="SYSTEM", case_id=case_id, model_id=self.sarvam.cfg["stt"]["model"], payload={
                "purpose": "stt", "ok": True, "language": lang, "stt_confidence": confidence, "low_confidence": low,
                "stt_latency_ms": round(lat["stt_ms"], 1), "redact_latency_ms": round(lat["redact_ms"], 1), "audio_bytes": len(wav)})
        return VoiceResult(False, None, None, red.text, lang, confidence, low, red.secret_shared, event_id, ins.inserted, intent, lat)

    # -- outbound -----------------------------------------------------------------------------------------------------
    def synthesize(self, text: str, language: str) -> tuple[bytes, float]:
        """Text that has already passed the grounding gate -> audio. Returns (wav bytes, latency in ms)."""
        sw = Stopwatch()
        audio = self.sarvam.tts(text, _TTS_CODE.get(language, "hi-IN"))
        return audio, sw.elapsed_ms()
