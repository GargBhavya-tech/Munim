"""Soundbox prop client (#133): subscribes to the merchant event channel, announces payments through the grounding gate.

Hardware (Pi/ESP32/phone speaker) plugs in through four injected callables, so the software is testable without it:
  transport.poll(cursor) -> list[event dict]   raises ConnectionError / TimeoutError on a flaky link
  tts(text, language) -> (audio, latency_ms)   e.g. VoiceTurnProcessor.synthesize
  play(audio)                                  the speaker
  transcribe(wav) -> str                       speech-to-text for the push-to-talk button
Events: {"event_id", "seq", "type", "merchant_id", "payload", "ts_sim"}. Delivery may repeat after a reconnect; announcements dedupe by event_id.
Time comes only from the injected SimClock. Backoff is in sim time: ``tick()`` does nothing until the clock reaches the next attempt.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta

from core.engine.clock import SimClock
from core.gate.pipeline import SEND, GroundingGate
from core.gate.sources import Claim, SourceBook
from core.voice.readback import ReadBackGate, format_rupees

ANNOUNCE_TYPES = {"PAYMENT_SUCCESS"}
BACKOFF_START, BACKOFF_MAX = timedelta(seconds=1), timedelta(seconds=30)
SEEN_LIMIT = 2048


@dataclass(frozen=True)
class Announcement:
    event_id: str
    text: str
    received_at: object
    announced_at: object

    @property
    def latency_s(self) -> float:
        return (self.announced_at - self.received_at).total_seconds()


class SoundboxProp:
    def __init__(self, merchant_id: str, transport, gate: GroundingGate, clock: SimClock, *, tts, play, language: str = "hinglish",
                 readback: ReadBackGate | None = None, transcribe=None, submit=None, case_id: str | None = None):
        self.merchant_id, self.transport, self.gate, self.clock = merchant_id, transport, gate, clock
        self.tts, self.play, self.language = tts, play, language
        self.readback, self.transcribe, self.submit, self.case_id = readback, transcribe, submit, case_id
        self.cursor = 0
        self.announcements: list[Announcement] = []
        self.silenced: list[tuple[str, str]] = []            # (event_id, gate status) for events the gate would not let us say
        self.reconnects = 0
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._backoff = BACKOFF_START
        self._next_attempt = clock.now()

    # -- channel ------------------------------------------------------------------------------------------------------
    def tick(self) -> int:
        """One poll. Returns the number of announcements made. On a link failure, waits (sim time) with doubling backoff before the next try."""
        if self.clock.now() < self._next_attempt:
            return 0
        try:
            events = self.transport.poll(self.cursor)
        except (ConnectionError, TimeoutError, OSError):
            self._next_attempt = self.clock.now() + self._backoff
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)
            self.reconnects += 1
            return 0
        self._backoff = BACKOFF_START
        made = 0
        for ev in events:
            self.cursor = max(self.cursor, int(ev.get("seq", 0)))
            made += self._handle(ev)
        return made

    def _handle(self, ev: dict) -> int:
        eid = ev["event_id"]
        if eid in self._seen:
            return 0                                          # replayed after a reconnect: never announce twice
        self._seen[eid] = None
        if len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        if ev.get("type") not in ANNOUNCE_TYPES or ev.get("merchant_id") not in (None, self.merchant_id):
            return 0
        received = self.clock.now()
        src = f"tool:payment_event#{eid}"
        paise = int(ev["payload"]["amount_paise"])
        book = SourceBook()
        book.add_tool_result(src, {"event_id": eid, "status": "SUCCESS", "amount_paise": paise})
        claim = Claim("pay", f"The payment event amount is {format_rupees(paise).lstrip(chr(8377)).replace(',', '')} rupees.", src)
        out = self.gate.run(claims=[claim], book=book, language=self.language, case_id=self.case_id)
        if out.status != SEND or not out.text:
            self.silenced.append((eid, out.status))           # unverified is not spoken
            return 0
        self.speak(out.text)
        self.announcements.append(Announcement(eid, out.text, received, self.clock.now()))
        return 1

    def speak(self, text: str) -> None:
        """Only text that has passed the gate may be passed here."""
        audio, _ms = self.tts(text, self.language)
        self.play(audio)

    # -- button -------------------------------------------------------------------------------------------------------
    def push_to_talk(self, wav: bytes):
        """Button released with a clip. The transcript can only answer a pending read-back; it is never an action by itself.
        Returns the read-back Resolution, or None when nothing is pending / no transcription is wired."""
        if self.transcribe is None or self.readback is None:
            return None
        res = self.readback.resolve(self.case_id, self.merchant_id, self.transcribe(wav))
        if res.status == "CONFIRMED" and self.submit is not None:
            self.submit(res.action, res.args)                 # the caller routes this through core.actions.executor
        return res

    def say_brief(self, brief) -> bool:
        """Speak a built MorningBrief (gate-verified text), then each read-back prompt. Executes nothing."""
        if brief.text is None:
            return False
        self.speak(brief.text)
        for h in brief.holds:
            self.speak(h.prompt)
        return True
