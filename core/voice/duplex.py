"""Full-duplex voice turn-taking (#116): barge-in over the push-to-talk pipeline, with the read-back rule unchanged.

A pure state machine. It owns no audio device: time comes from the injected ``SimClock`` and the merchant's microphone arrives as injected
``Frame`` objects (a frame is ``ms`` of audio already classified speech / not speech by a VAD). ``pump`` advances the clock by the frame's length
and then looks at the frame, so a test (or a real driver) controls time completely.

Rules enforced here, in code:
  * Only sentences that passed the grounding gate (``verified=True``) can be spoken. An unverified sentence is refused at ``speak``.
  * Barge-in: speech for ``barge_in_min_ms`` while Munim is speaking cancels the speaking stream at once. The deadline from speech onset
    to cancel is ``barge_in_deadline_ms`` and the constructor refuses a config that cannot meet it.
  * A sentence counts as SAID only when it was fully heard (playback position, minus the playout lag, reached its end). The sentence cut off
    by a barge-in is INTERRUPTED and never delivered; sentences not reached are UNPLAYED. Callers use ``delivered`` as "what Munim said".
  * Overlapping speech never confirms an action. A held read-back (#40) is resolved only by an utterance whose first speech frame came after the
    read-back prompt was fully delivered. Otherwise the gate is not consulted and the prompt must be replayed.
Thresholds live in config/voice.yaml under ``duplex`` when present; the defaults below are [ASSUMPTION] until measured on real shop audio.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from core.engine.clock import SimClock
from core.voice.pipeline import load_voice_config

DEFAULTS = {"barge_in_min_ms": 100, "barge_in_deadline_ms": 200, "endpoint_ms": 70, "playout_lag_ms": 40}

IDLE, SPEAKING, LISTENING, AWAIT_TRANSCRIPT = "IDLE", "SPEAKING", "LISTENING", "AWAIT_TRANSCRIPT"


@dataclass(frozen=True)
class Sentence:
    sentence_id: str
    text: str
    duration_ms: int
    verified: bool = False                   # True only if the sentence passed the grounding gate
    readback_id: str | None = None           # set on the read-back prompt of a held action (#40)


@dataclass(frozen=True)
class Frame:
    speech: bool                             # VAD verdict for this frame
    ms: int = 20


@dataclass(frozen=True)
class DuplexEvent:
    kind: str                                # BARGE_IN | END_OF_TURN | PLAYBACK_DONE
    at: datetime
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TurnOutcome:
    kind: str                                # TURN | READBACK_RESOLVED | READBACK_REPLAY | EMPTY
    text: str = ""
    confirms: bool = False                   # True only when a read-back was resolved CONFIRMED by non-overlapping speech
    resolution: object | None = None
    replay: Sentence | None = None           # the read-back prompt to speak again


class DuplexSession:
    def __init__(self, clock: SimClock, *, readback=None, config: dict | None = None, cancel_stream: Callable[[datetime], None] | None = None,
                 on_delivered: Callable[[Sentence], None] | None = None):
        cfg = dict(DEFAULTS)
        cfg.update((config if config is not None else load_voice_config()).get("duplex", {}) or {})
        self.min_ms, self.deadline_ms = int(cfg["barge_in_min_ms"]), int(cfg["barge_in_deadline_ms"])
        self.endpoint_ms, self.lag_ms = int(cfg["endpoint_ms"]), int(cfg["playout_lag_ms"])
        if self.min_ms > self.deadline_ms:
            raise ValueError("barge_in_min_ms cannot exceed barge_in_deadline_ms: the stream could not be cancelled in time")
        self.clock, self.readback = clock, readback
        self._cancel_stream, self._on_delivered = cancel_stream, on_delivered
        self.state = IDLE
        self._queue: list[Sentence] = []
        self._started: datetime | None = None
        self._n_delivered = 0                       # how many of _queue have been fully heard
        self.delivered: list[Sentence] = []
        self.interrupted: Sentence | None = None
        self.unplayed: list[Sentence] = []
        self.cancel_latency_ms: int | None = None
        self._readback: Sentence | None = None      # prompt of the held action awaiting an answer
        self._readback_heard_at: datetime | None = None
        self._speech_ms = 0
        self._silence_ms = 0
        self._onset: datetime | None = None
        self._utt_onset: datetime | None = None

    # -- Munim speaks -------------------------------------------------------------------------------------------------
    def speak(self, sentences: list[Sentence]) -> None:
        for s in sentences:
            if not s.verified:
                raise ValueError(f"sentence {s.sentence_id} is not verified: nothing unverified is said")
            if s.duration_ms <= 0:
                raise ValueError("duration_ms must be positive")
        self._sync()
        if self.state == SPEAKING:
            raise ValueError("already speaking: wait for PLAYBACK_DONE or a barge-in")
        self._queue, self._started, self._n_delivered = list(sentences), self.clock.now(), 0
        self.interrupted, self.unplayed, self.cancel_latency_ms = None, [], None
        self._readback = next((s for s in sentences if s.readback_id), self._readback)
        if self._readback in sentences:
            self._readback_heard_at = None
        self.state = SPEAKING if sentences else IDLE
        self._reset_listen()

    def _heard_ms(self) -> int:
        """How much of the queue the merchant has actually heard (playback position minus the playout lag)."""
        if self._started is None:
            return 0
        elapsed = int((self.clock.now() - self._started) / timedelta(milliseconds=1))
        return max(0, elapsed - self.lag_ms)

    def _end_offsets(self) -> list[int]:
        out, t = [], 0
        for s in self._queue:
            t += s.duration_ms
            out.append(t)
        return out

    def _sync(self) -> list[DuplexEvent]:
        """Mark sentences fully heard so far as delivered; finish playback when the queue is done."""
        if self.state != SPEAKING:
            return []
        heard, ends, events = self._heard_ms(), self._end_offsets(), []
        while self._n_delivered < len(self._queue) and ends[self._n_delivered] <= heard:
            s = self._queue[self._n_delivered]
            self._mark_delivered(s, self._started + timedelta(milliseconds=ends[self._n_delivered] + self.lag_ms))
            self._n_delivered += 1
        if self._n_delivered == len(self._queue):
            self.state = IDLE
            events.append(DuplexEvent("PLAYBACK_DONE", self.clock.now()))
        return events

    def _mark_delivered(self, s: Sentence, heard_at: datetime) -> None:
        self.delivered.append(s)
        if s.readback_id and self._readback is not None and s.readback_id == self._readback.readback_id:
            self._readback_heard_at = heard_at
        if self._on_delivered:
            self._on_delivered(s)

    # -- the merchant's microphone --------------------------------------------------------------------------------------
    def pump(self, frame: Frame) -> list[DuplexEvent]:
        start = self.clock.now()
        self.clock.advance(timedelta(milliseconds=frame.ms))
        events = self._sync()
        if frame.speech:
            self._silence_ms = 0
            if self._speech_ms == 0:
                self._onset = start
            self._speech_ms += frame.ms
            if self.state == IDLE:
                self.state = LISTENING
            if self._utt_onset is None:
                self._utt_onset = self._onset
            if self.state == SPEAKING and self._speech_ms >= self.min_ms:
                events.append(self._barge_in())
        else:
            if self.state == SPEAKING and self._speech_ms:
                self._speech_ms, self._onset, self._utt_onset = 0, None, None     # a blip shorter than the debounce is noise
            elif self.state == LISTENING:
                self._silence_ms += frame.ms
                if self._silence_ms >= self.endpoint_ms:
                    self.state = AWAIT_TRANSCRIPT
                    events.append(DuplexEvent("END_OF_TURN", self.clock.now()))
        return events

    def _barge_in(self) -> DuplexEvent:
        now = self.clock.now()
        heard, ends = self._heard_ms(), self._end_offsets()
        i = self._n_delivered
        self.interrupted = self._queue[i] if i < len(self._queue) and (ends[i] - self._queue[i].duration_ms) < heard else None
        self.unplayed = self._queue[i + (1 if self.interrupted else 0):]
        self.cancel_latency_ms = int((now - self._onset) / timedelta(milliseconds=1))
        if self._cancel_stream:
            self._cancel_stream(now)
        self.state = LISTENING
        self._queue, self._started = [], None
        return DuplexEvent("BARGE_IN", now, {"cancel_latency_ms": self.cancel_latency_ms, "interrupted": self.interrupted.sentence_id if self.interrupted else None,
                                             "unplayed": [s.sentence_id for s in self.unplayed]})

    def resay(self) -> list[Sentence]:
        """What was cut off or never reached, in order. The caller re-checks it before speaking it again."""
        return ([self.interrupted] if self.interrupted else []) + list(self.unplayed)

    # -- the finished utterance ------------------------------------------------------------------------------------------
    def on_transcript(self, text: str, case_id: str | None, merchant_id: str) -> TurnOutcome:
        onset = self._utt_onset
        self._reset_listen()
        self.state = IDLE if self.state != SPEAKING else SPEAKING
        if not (text or "").strip():
            return TurnOutcome("EMPTY")
        if self._readback is None:
            return TurnOutcome("TURN", text)
        heard_at = self._readback_heard_at
        if heard_at is None or onset is None or onset < heard_at:
            return TurnOutcome("READBACK_REPLAY", text, False, None, self._readback)     # overlap or prompt not fully heard: never a yes
        if self.readback is None:
            return TurnOutcome("TURN", text)
        res = self.readback.resolve(case_id, merchant_id, text)
        if res.status in ("CONFIRMED", "DECLINED"):
            self._readback = None
        return TurnOutcome("READBACK_RESOLVED", text, res.status == "CONFIRMED", res)

    def _reset_listen(self) -> None:
        self._speech_ms, self._silence_ms, self._onset, self._utt_onset = 0, 0, None, None
