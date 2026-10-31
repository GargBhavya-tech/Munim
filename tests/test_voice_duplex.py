"""#116 full-duplex voice. Real DuplexSession, real ReadBackGate, injected SimClock and injected frames. No audio device, no Sarvam."""
import pytest

from core.voice.duplex import DuplexSession, Frame, Sentence
from core.voice.readback import ReadBackGate

CFG = {"duplex": {"barge_in_min_ms": 100, "barge_in_deadline_ms": 200, "endpoint_ms": 60, "playout_lag_ms": 40}}


@pytest.fixture
def world(db_path, clock):
    from tests.world import World
    return World(db_path, clock, {})


def S(i, ms=1000, **kw):
    return Sentence(f"S{i}", f"sentence {i}", ms, verified=True, **kw)


def run(d, speech, ms):
    ev = []
    for _ in range(ms // 20):
        ev += d.pump(Frame(speech, 20))
    return ev


def sess(clock, **kw):
    return DuplexSession(clock, config=CFG, **kw)


def test_unverified_sentence_cannot_be_spoken(clock):
    d = sess(clock)
    with pytest.raises(ValueError):
        d.speak([Sentence("X", "unproven", 500, verified=False)])


def test_config_that_cannot_meet_the_deadline_is_refused(clock):
    with pytest.raises(ValueError):
        DuplexSession(clock, config={"duplex": {"barge_in_min_ms": 300, "barge_in_deadline_ms": 200}})


def test_uninterrupted_playback_delivers_every_sentence_in_order(clock):
    seen = []
    d = sess(clock, on_delivered=lambda s: seen.append(s.sentence_id))
    d.speak([S(1), S(2)])
    ev = run(d, False, 2100)
    assert [s.sentence_id for s in d.delivered] == ["S1", "S2"] == seen
    assert [e.kind for e in ev] == ["PLAYBACK_DONE"] and d.state == "IDLE"


def test_barge_in_cancels_within_the_bound_and_the_cut_sentence_is_not_delivered(clock):
    cancelled = []
    d = sess(clock, cancel_stream=cancelled.append)
    d.speak([S(1), S(2), S(3)])
    run(d, False, 1500)                      # S1 fully heard, S2 half way
    ev = run(d, True, 200)
    barge = [e for e in ev if e.kind == "BARGE_IN"]
    assert len(barge) == 1 and len(cancelled) == 1
    assert d.cancel_latency_ms == 100 <= 200
    assert [s.sentence_id for s in d.delivered] == ["S1"]          # S2 was cut off: NOT delivered
    assert d.interrupted.sentence_id == "S2" and [s.sentence_id for s in d.unplayed] == ["S3"]
    assert [s.sentence_id for s in d.resay()] == ["S2", "S3"]
    assert d.state == "LISTENING"


def test_sentence_that_finished_but_is_still_in_the_playout_buffer_is_not_delivered(clock):
    d = sess(clock)
    d.speak([S(1, 1000), S(2, 1000)])
    run(d, False, 920)
    run(d, True, 100)                        # cancel at 1020 elapsed - 40 lag = 980 heard: S1 (1000) not yet fully heard
    assert d.delivered == [] and d.interrupted.sentence_id == "S1"


def test_a_short_blip_is_noise_and_does_not_interrupt(clock):
    d = sess(clock)
    d.speak([S(1, 1000)])
    run(d, True, 60)
    run(d, False, 40)
    run(d, True, 60)                         # two blips, each under the 100 ms debounce
    assert d.state == "SPEAKING" and d.interrupted is None
    run(d, False, 1000)
    assert [s.sentence_id for s in d.delivered] == ["S1"]


def test_end_of_turn_after_the_endpoint_silence(clock):
    d = sess(clock)
    run(d, True, 200)
    ev = run(d, False, 80)
    assert [e.kind for e in ev] == ["END_OF_TURN"] and d.state == "AWAIT_TRANSCRIPT"


# --- read-back (#40) stays enforced ------------------------------------------------------------------------------------------
def held(world, gate):
    h = gate.review("C-1", "M-0006", "create_device_replacement", {}, low_confidence=True, language="hinglish")
    assert h.outcome == "HOLD"
    return h


def prompt_sentence(h, ms=1500):
    return Sentence("RB", h.prompt, ms, verified=True, readback_id=h.confirmation_id)


def test_overlapping_speech_does_not_confirm_the_held_action(world, clock):
    gate = ReadBackGate(world.conn, clock, world.registry)
    h = held(world, gate)
    d = sess(clock, readback=gate)
    d.speak([prompt_sentence(h)])
    run(d, False, 400)
    run(d, True, 200)                        # merchant says "haan" over the prompt
    run(d, False, 80)
    out = d.on_transcript("haan", "C-1", "M-0006")
    assert out.kind == "READBACK_REPLAY" and out.confirms is False and out.replay.readback_id == h.confirmation_id
    assert gate.pending("C-1", "M-0006")     # still held, nothing confirmed
    assert d.delivered == []                 # the prompt was never fully heard


def test_yes_after_the_prompt_was_fully_heard_confirms_once(world, clock):
    gate = ReadBackGate(world.conn, clock, world.registry)
    h = held(world, gate)
    d = sess(clock, readback=gate)
    d.speak([prompt_sentence(h)])
    run(d, False, 1700)
    assert [s.sentence_id for s in d.delivered] == ["RB"]
    run(d, True, 200)
    run(d, False, 80)
    out = d.on_transcript("haan", "C-1", "M-0006")
    assert out.kind == "READBACK_RESOLVED" and out.confirms and out.resolution.status == "CONFIRMED"
    assert out.resolution.action == "create_device_replacement"


def test_speech_starting_inside_the_playout_lag_does_not_confirm(world, clock):
    gate = ReadBackGate(world.conn, clock, world.registry)
    h = held(world, gate)
    d = sess(clock, readback=gate)
    d.speak([prompt_sentence(h, 1000)])
    run(d, False, 1020)                      # prompt not yet heard (lag)
    run(d, True, 40)
    run(d, False, 40)
    run(d, True, 40)
    run(d, False, 80)
    assert d.on_transcript("haan", "C-1", "M-0006").confirms is False


def test_no_after_full_prompt_declines(world, clock):
    gate = ReadBackGate(world.conn, clock, world.registry)
    h = held(world, gate)
    d = sess(clock, readback=gate)
    d.speak([prompt_sentence(h)])
    run(d, False, 1700)
    run(d, True, 200)
    run(d, False, 80)
    out = d.on_transcript("nahi", "C-1", "M-0006")
    assert out.resolution.status == "DECLINED" and not out.confirms


def test_barge_in_during_the_prompt_then_replay_then_yes_confirms(world, clock):
    gate = ReadBackGate(world.conn, clock, world.registry)
    h = held(world, gate)
    d = sess(clock, readback=gate)
    p = prompt_sentence(h)
    d.speak([p])
    run(d, False, 300)
    run(d, True, 200)
    run(d, False, 80)
    out = d.on_transcript("haan", "C-1", "M-0006")
    assert out.replay == p
    d.speak([out.replay])
    run(d, False, 1700)
    run(d, True, 200)
    run(d, False, 80)
    assert d.on_transcript("haan", "C-1", "M-0006").confirms


def test_plain_turn_without_a_held_action_passes_text_through(clock):
    d = sess(clock)
    run(d, True, 200)
    run(d, False, 80)
    out = d.on_transcript("mera soundbox band hai", None, "M-0006")
    assert out.kind == "TURN" and out.text == "mera soundbox band hai" and not out.confirms
    assert d.on_transcript("  ", None, "M-0006").kind == "EMPTY"
