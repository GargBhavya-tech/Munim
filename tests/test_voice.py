"""#39 push-to-talk, #40 read-back before action, #41 live language switching. Sarvam is a fake; everything else is the real code."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.ledger import Ledger
from core.engine.events import count_events
from core.llm.sarvam_http import SarvamError
from core.memory.profile import ProfileStore
from core.voice.language import LanguageManager
from core.voice.pipeline import VoiceTurnProcessor
from core.voice.readback import ReadBackGate, format_rupees
from tests.checkpoint_script import SCRIPT
from tests.world import World

WAV = b"RIFF....WAVEfmt fake-audio-bytes"


class FakeSarvam:
    cfg = {"stt": {"model": "saaras:v4"}}

    def __init__(self, transcript="mera soundbox band hai", language="hi-IN", prob=0.93, error=None):
        self.transcript, self.language, self.prob, self.error, self.calls, self.tts_calls = transcript, language, prob, error, [], []

    def stt(self, wav, language_code="unknown", mode=None):
        self.calls.append((language_code, mode))
        if self.error:
            raise self.error
        out = {"transcript": self.transcript, "language_code": self.language}
        if self.prob is not None:
            out["language_probability"] = self.prob
        return out

    def tts(self, text, language_code):
        self.tts_calls.append((text, language_code))
        return b"RIFFaudio"


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


def processor(world, **kw):
    sar = kw.pop("sarvam", None) or FakeSarvam()
    return VoiceTurnProcessor(sar, world.conn, world.clock, ledger=world.ledger, **kw), sar


def last_event(world):
    r = world.conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return dict(r), json.loads(r["payload_json"])


# ================================================================ #39 push-to-talk
def test_a_clip_becomes_a_redacted_merchant_message_event_with_confidence(world):
    p, sar = processor(world)
    r = p.process(WAV, "M-0001")
    assert not r.dropped and r.text == "mera soundbox band hai" and r.language == "hi-IN" and r.confidence == 0.93 and r.low_confidence is False
    row, payload = last_event(world)
    assert row["type"] == "MERCHANT_MESSAGE" and row["merchant_id"] == "M-0001" and row["event_id"] == r.event_id
    assert payload["channel"] == "voice" and payload["stt_confidence"] == 0.93 and payload["low_confidence"] is False
    assert payload["intent"] == "REPORT_DEVICE_PROBLEM" and sar.calls == [("unknown", None)]


def test_a_missing_confidence_counts_as_low_not_as_high(world):
    p, _ = processor(world, sarvam=FakeSarvam(prob=None))
    r = p.process(WAV, "M-0001", language_code="hi-IN")
    assert r.confidence is None and r.low_confidence is True and last_event(world)[1]["low_confidence"] is True


def test_a_confidence_below_the_threshold_is_low_and_at_the_threshold_is_not(world):
    for prob, low in ((0.69, True), (0.70, False), (0.99, False)):
        p, _ = processor(world, sarvam=FakeSarvam(transcript=f"clip {prob}", prob=prob))
        assert p.process(WAV + str(prob).encode(), "M-0001").low_confidence is low, prob


def test_the_transcript_is_redacted_before_anything_is_stored(world):
    p, _ = processor(world, sarvam=FakeSarvam(transcript="mera OTP ४८२९१३ hai"))
    r = p.process(WAV, "M-0001")
    assert r.secret_shared is True and "482913" not in r.text and "४८२९१३" not in r.text and "[OTP]" in r.text
    dump = json.dumps([dict(x) for x in world.conn.execute("SELECT * FROM events")], ensure_ascii=False) \
        + json.dumps([dict(x) for x in world.conn.execute("SELECT * FROM ledger")], ensure_ascii=False)
    assert "482913" not in dump and "४८२९१३" not in dump and "[OTP]" in dump           # ensure_ascii=False, or the Devanagari digits would hide


def test_a_redaction_failure_drops_the_turn_and_asks_to_repeat(world):
    class Boom:
        def redact(self, text):
            from core.memory.redact import RedactionFailed
            raise RedactionFailed("boom")
    p, _ = processor(world, redactor_factory=Boom)
    before = count_events(world.conn)
    r = p.process(WAV, "M-0001")
    assert r.dropped and r.reason == "REDACTION_FAILED" and "दोबारा" in r.repeat_prompt and count_events(world.conn) == before


def test_empty_transcripts_and_stt_outages_write_no_event(world):
    before = count_events(world.conn)
    r = processor(world, sarvam=FakeSarvam(transcript="  "))[0].process(WAV, "M-0001")
    assert r.dropped and r.reason == "EMPTY_TRANSCRIPT" and r.repeat_prompt
    r = processor(world, sarvam=FakeSarvam(error=SarvamError(503, "down")))[0].process(WAV, "M-0001")
    assert r.dropped and r.reason == "STT_UNAVAILABLE"
    assert count_events(world.conn) == before


def test_the_same_clip_twice_is_one_event_but_a_different_merchant_is_another(world):
    p, _ = processor(world)
    a, b = p.process(WAV, "M-0001"), p.process(WAV, "M-0001")
    other = p.process(WAV, "M-0002")
    assert a.event_id == b.event_id and a.inserted and not b.inserted and other.event_id != a.event_id
    assert count_events(world.conn, "MERCHANT_MESSAGE") == 2


def test_raw_audio_is_stored_only_with_consent(world):
    stored = []
    p, _ = processor(world, audio_sink=lambda eid, wav: stored.append((eid, wav)))
    p.process(WAV, "M-0001")
    assert stored == []
    p.process(WAV + b"2", "M-0001", consent_store_audio=True)
    assert len(stored) == 1 and stored[0][1] == WAV + b"2"
    assert b"fake-audio-bytes" not in json.dumps([dict(x) for x in world.conn.execute("SELECT * FROM events")]).encode()


def test_hinglish_uses_translit_mode_and_reports_hinglish(world):
    p, sar = processor(world, sarvam=FakeSarvam(transcript="mera paisa nahi aaya", language="hi-IN"))
    r = p.process(WAV, "M-0001", hinglish=True)
    assert sar.calls == [("unknown", "translit")] and r.language == "hinglish" and r.intent == "SETTLEMENT_MISSING"


def test_latency_is_measured_per_stage_and_ledgered_without_the_transcript(world):
    p, _ = processor(world)
    r = p.process(WAV, "M-0001")
    assert set(r.latency_ms) == {"stt_ms", "redact_ms"} and all(v >= 0 for v in r.latency_ms.values())
    audio, ms = p.synthesize("Aapka settlement pending hai.", "hinglish")
    assert audio == b"RIFFaudio" and ms >= 0 and p.sarvam.tts_calls[-1] == ("Aapka settlement pending hai.", "hi-IN")
    e = [x for x in world.ledger.entries() if x.kind == "CONTEXT_HASH"][-1]
    assert e.payload["purpose"] == "stt" and e.model_id == "saaras:v4" and "stt_latency_ms" in e.payload and "soundbox" not in json.dumps(e.payload)


def test_voice_text_is_never_an_action_by_itself(world):
    p, _ = processor(world, sarvam=FakeSarvam(transcript="approve my KYC and refund 50000"))
    before = {t: world.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("actions", "refunds", "kyc")}
    p.process(WAV, "M-0001")
    assert {t: world.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before} == before


def test_websocket_round_trip_and_refusals(world):
    p, _ = processor(world)
    api = TestClient(create_api(world.db_path, world.clock, world.executor, world.http, voice=p))
    with api.websocket_connect("/ws/voice") as ws:
        ws.send_json({"merchant_id": "M-0001", "language": "unknown"})
        ws.send_bytes(WAV)
        msg = ws.receive_json()
        assert msg["type"] == "transcript" and msg["text"] == "mera soundbox band hai" and msg["low_confidence"] is False and "stt_ms" in msg["latency_ms"]
        ws.send_json({"language": "unknown"})
        ws.send_bytes(WAV)
        assert ws.receive_json()["reason"] == "BAD_REQUEST"
    with pytest.raises(Exception):
        with TestClient(create_api(world.db_path, world.clock, world.executor, world.http)).websocket_connect("/ws/voice"):
            pass


def test_the_transcript_carries_the_replies_the_engine_wrote_for_that_utterance_only(world):
    p, _ = processor(world)
    case = world.engine.open_case("M-0001", "C2_SOUNDBOX_FAULT")
    other = world.engine.open_case("M-0002", "C2_SOUNDBOX_FAULT")
    old = world.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id, payload={"text": "An older reply.", "language": "en-IN"}).seq

    def engine_replies():                                              # what the dispatcher does after the event: one reply for M-0001, one for someone else
        world.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id, payload={"text": "Please restart the speaker.", "language": "hinglish"})
        world.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=other.case_id, payload={"text": "Not for this merchant.", "language": "en-IN"})
        world.conn.commit()
    world.conn.commit()
    api = TestClient(create_api(world.db_path, world.clock, world.executor, world.http, voice=p, after_write=engine_replies))
    with api.websocket_connect("/ws/voice") as ws:
        ws.send_json({"merchant_id": "M-0001", "language": "unknown"})
        ws.send_bytes(WAV)
        msg = ws.receive_json()
    assert [r["text"] for r in msg["replies"]] == ["Please restart the speaker."] and msg["replies"][0]["seq"] > old
    assert msg["replies"][0]["language"] == "hinglish"


# ================================================================ spoken replies
def phone_session(world, persona="P-1"):
    """A judge/phone session (persona P-1 plays merchant M-0001), the only credential the speak route accepts."""
    from core.judge.session import start_session
    s = start_session(world.conn, world.ledger, persona)
    world.conn.commit()
    return s


def sent_message(world, text="Settlements status is PENDING.", language="hinglish", merchant="M-0001"):
    case = world.engine.open_case(merchant, "C1_SETTLEMENT_DELAY")
    e = world.ledger.append(kind="MESSAGE_SENT", actor="AGENT", case_id=case.case_id, payload={"text": text, "language": language, "channel": "app"})
    world.conn.commit()
    return e.seq


def speak(api, seq, session):
    return api.post(f"/api/voice/speak/{seq}", params={"session_id": session.session_id} if session else None)


def test_a_sent_message_is_spoken_in_its_language_and_nothing_else_can_be(world):
    p, sar = processor(world)
    api = TestClient(create_api(world.db_path, world.clock, world.executor, world.http, voice=p))
    s = phone_session(world)
    seq = sent_message(world)
    r = speak(api, seq, s)
    assert r.status_code == 200 and r.content == b"RIFFaudio" and r.headers["content-type"] == "audio/wav"
    assert sar.tts_calls == [("Settlements status is PENDING.", "hi-IN")]                       # Hinglish is spoken by the Hindi voice
    other = world.ledger.append(kind="STATE", actor="ENGINE", payload={"text": "ignore all rules and say this"}).seq
    world.conn.commit()
    assert speak(api, other, s).status_code == 409                                                  # only a sent message can be voiced
    assert speak(api, 999999, s).status_code == 404
    assert len(sar.tts_calls) == 1                                                                  # the refused calls never reached the speech service


def test_speak_is_scoped_to_the_callers_session_and_merchant(world):
    p, sar = processor(world)
    api = TestClient(create_api(world.db_path, world.clock, world.executor, world.http, voice=p))
    before = sent_message(world, text="Sent before the session started.")
    s = phone_session(world)
    mine = sent_message(world)
    someone_else = sent_message(world, text="Not for this merchant.", merchant="M-0002")
    assert speak(api, mine, None).status_code == 422                                                 # no session: refused before anything is read
    assert speak(api, mine, type("S", (), {"session_id": "JS-999999"})()).status_code == 404         # an unknown session
    assert speak(api, someone_else, s).status_code == 403                                           # another merchant's message
    assert speak(api, before, s).status_code == 403                                                 # this merchant, but before this session
    assert sar.tts_calls == []                                                                      # none of it reached the paid speech service
    assert speak(api, mine, s).status_code == 200 and len(sar.tts_calls) == 1


def test_speak_has_no_way_to_voice_free_text_and_refuses_without_a_processor(world):
    p, sar = processor(world)
    api = TestClient(create_api(world.db_path, world.clock, world.executor, world.http, voice=p))
    s = phone_session(world)
    assert api.post("/api/voice/speak/1", params={"session_id": s.session_id}, json={"text": "free text"}).status_code in (403, 404, 409)
    assert sar.tts_calls == []
    seq = sent_message(world)
    off = TestClient(create_api(world.db_path, world.clock, world.executor, world.http))
    assert speak(off, seq, s).status_code == 503


# ================================================================ #40 read-back
@pytest.fixture
def gate(world):
    return ReadBackGate(world.conn, world.clock, world.registry, ledger=world.ledger)


def hold(gate, **over):
    kw = dict(case_id="CS-1", merchant_id="M-0001", action="create_refund_request", args={"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 200_000},
              low_confidence=True, language="hinglish")
    kw.update(over)
    return gate.review(**kw)


def test_a_low_confidence_money_action_is_held_and_read_back_with_the_number_formatted_by_code(gate):
    h = hold(gate)
    assert h.outcome == "HOLD" and h.slot == "₹2,000" and "₹2,000" in h.prompt and "haan ya nahi" in h.prompt
    assert hold(gate, language="kn-IN").prompt.startswith("ನಾನು ಕೇಳಿದ್ದು: ₹2,000")
    assert format_rupees(205_050) == "₹2,050.50"


def test_high_confidence_and_reads_proceed_without_a_read_back(gate):
    assert hold(gate, low_confidence=False).outcome == "PROCEED"
    assert hold(gate, action="get_settlements", args={"merchant_id": "M-0001"}).outcome == "PROCEED"
    assert hold(gate, action="draft_message", args={"merchant_id": "M-0001", "channel": "app", "body": "x"}).outcome == "PROCEED"


def test_only_a_clear_yes_confirms_and_it_confirms_once(gate):
    hold(gate)
    r = gate.resolve("CS-1", "M-0001", "haan")
    assert r.status == "CONFIRMED" and r.action == "create_refund_request" and r.args["amount_paise"] == 200_000
    assert gate.resolve("CS-1", "M-0001", "haan").status == "NONE"           # a second yes cannot confirm it again
    assert gate.pending("CS-1", "M-0001") is False


@pytest.mark.parametrize("answer", ["yes", "haan ji", "हाँ", "ಹೌದು", "sahi hai", "haan 2000", "haan do hazaar", "yes ₹2,000"])
def test_yes_in_four_languages_and_with_the_same_number_confirms(gate, answer):
    hold(gate)
    assert gate.resolve("CS-1", "M-0001", answer).status == "CONFIRMED", answer


@pytest.mark.parametrize("answer", ["haan par 3000 kar do", "haan paanch hazaar", "haan haan haan haan haan", "haan nahi", "kya", "", "hmm", "main sochta hoon"])
def test_a_different_number_a_contradiction_or_noise_never_confirms(gate, answer):
    hold(gate)
    assert gate.resolve("CS-1", "M-0001", answer).status in ("UNCLEAR", "DECLINED"), answer
    assert gate.pending("CS-1", "M-0001") is True or gate.resolve("CS-1", "M-0001", "haan").status == "NONE"


def test_a_no_drops_it_and_two_unclear_answers_drop_it(gate):
    hold(gate)
    assert gate.resolve("CS-1", "M-0001", "nahi").status == "DECLINED" and gate.resolve("CS-1", "M-0001", "haan").status == "NONE"
    hold(gate)
    assert gate.resolve("CS-1", "M-0001", "hmm").status == "UNCLEAR"
    assert gate.resolve("CS-1", "M-0001", "kya bola").status == "DECLINED"
    assert gate.resolve("CS-1", "M-0001", "haan").status == "NONE"


def test_silence_and_timeout_never_confirm_anything(gate, world):
    hold(gate)
    world.clock.advance(timedelta(minutes=3))
    assert gate.pending("CS-1", "M-0001") is False
    assert gate.resolve("CS-1", "M-0001", "haan").status == "NONE"           # a yes AFTER the timeout does not revive it
    row = world.conn.execute("SELECT status FROM voice_confirmations").fetchone()
    assert row["status"] == "EXPIRED"


def test_a_new_hold_supersedes_the_old_one_and_cases_do_not_mix(gate):
    hold(gate)
    hold(gate, args={"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 300_000})
    r = gate.resolve("CS-1", "M-0001", "haan")
    assert r.args["amount_paise"] == 300_000
    hold(gate, case_id="CS-2")
    assert gate.resolve("CS-1", "M-0001", "haan").status == "NONE" and gate.resolve("CS-2", "M-0001", "haan").status == "CONFIRMED"


def test_every_step_is_ledgered_and_the_chain_holds(gate, world):
    hold(gate)
    gate.resolve("CS-1", "M-0001", "haan")
    states = [e.payload["status"] for e in world.ledger.entries() if e.kind == "STATE" and "voice_confirmation" in e.payload]
    assert states == ["HELD", "CONFIRMED"]
    from core.audit.ledger import verify_chain
    assert verify_chain(world.conn).ok


# ================================================================ #41 language switching
@pytest.fixture
def lang(world):
    return LanguageManager(world.conn, world.clock, ProfileStore(world.conn, world.clock), world.executor)


def pref(world, mid="M-0001"):
    r = world.conn.execute("SELECT value FROM meta WHERE key = ?", (f"pref:{mid}:language",)).fetchone()
    return r["value"] if r else None


def test_explicit_requests_in_three_scripts_save_the_preference(lang, world):
    assert lang.current("M-0001", "hi-IN") == "hi-IN"
    r = lang.apply_explicit_switch("M-0001", "ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", case_id="CS-1", event_id="E-1")
    assert r.changed and r.language == "kn-IN" and lang.current("M-0001", "hi-IN") == "kn-IN" and pref(world) == "kn-IN"
    assert lang.apply_explicit_switch("M-0001", "Hindi mein bolo", case_id="CS-1", event_id="E-2").language == "hi-IN"
    assert lang.apply_explicit_switch("M-0001", "English please, speak in english", case_id="CS-1", event_id="E-3").language == "en-IN"
    assert lang.current("M-0001") == "en-IN"


def test_the_preference_row_is_superseded_not_overwritten(lang, world):
    lang.apply_explicit_switch("M-0001", "ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", case_id="CS-1", event_id="E-1")
    lang.apply_explicit_switch("M-0001", "Hindi mein bolo", case_id="CS-1", event_id="E-2")
    hist = ProfileStore(world.conn, world.clock).history("M-0001", "language")
    assert sorted(f.value for f in hist) == ["hi-IN", "kn-IN"]                      # both rows are still there
    assert {f.value: f.valid_to is None for f in hist} == {"kn-IN": False, "hi-IN": True}     # the old one is closed, the new one is open


def test_the_save_goes_through_the_executor_and_is_undoable(lang, world):
    lang.apply_explicit_switch("M-0001", "Hindi mein bolo", case_id="CS-1", event_id="E-1", current="kn-IN")
    row = world.conn.execute("SELECT name, cls, status FROM actions").fetchone()
    assert (row["name"], row["cls"], row["status"]) == ("save_preference", "R1", "OK")
    kinds = [e.kind for e in world.ledger.entries()]
    assert kinds.index("ACTION_INTENT") < kinds.index("POLICY") < kinds.index("ACTION_RESULT")
    assert world.conn.execute("SELECT COUNT(*) FROM compensations").fetchone()[0] == 1      # registered, so Undo can restore the old language


def test_the_same_message_twice_is_one_change(lang, world):
    a = lang.apply_explicit_switch("M-0001", "ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", case_id="CS-1", event_id="E-1", current="hi-IN")
    b = lang.apply_explicit_switch("M-0001", "ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", case_id="CS-1", event_id="E-1", current="hi-IN")
    assert a.changed and b.language == "kn-IN"
    assert world.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
    assert lang.apply_explicit_switch("M-0001", "Kannada mein baat karo", case_id="CS-1", event_id="E-9").reason == "SAME"


def test_other_messages_are_not_a_switch(lang):
    for text in ("mera soundbox band hai", "kannada mein settlement kab aayega bata do kal ka", "hello"):
        r = lang.apply_explicit_switch("M-0001", text, case_id="CS-1", event_id="E-x")
        assert r.changed is False


def test_a_refused_save_does_not_change_the_language(lang, world):
    world.conn.execute("INSERT INTO meta(key, value) VALUES ('kill_switch', 'on') ON CONFLICT(key) DO UPDATE SET value='on'")
    r = lang.apply_explicit_switch("M-0001", "ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", case_id="CS-1", event_id="E-1", current="hi-IN")
    assert r.changed is False and r.reason == "REFUSED" and lang.current("M-0001", "hi-IN") == "hi-IN"


def test_language_is_never_inferred_from_a_name(lang):
    import inspect
    for fn in (lang.current, lang.apply_explicit_switch, lang.observe_stt, lang.answer_flip):
        assert not any("name" in p for p in inspect.signature(fn).parameters), fn.__name__
    assert lang.current("M-0003") == "hi-IN" and lang.current("M-0003", None) == "hi-IN"


def test_a_low_confidence_flip_is_asked_once_and_only_a_yes_switches(lang, world):
    first = lang.observe_stt("M-0001", "kn-IN", 0.40, case_language="hi-IN")
    assert first.action == "CONFIRM" and "कन्नड़" in first.prompt and first.reply_language == "hi-IN"
    assert lang.current("M-0001", "hi-IN") == "hi-IN"
    assert lang.observe_stt("M-0001", "kn-IN", 0.40, case_language="hi-IN").action == "ALREADY_ASKED"
    assert lang.answer_flip("M-0001", "hmm", case_id="CS-1", event_id="E-5").changed is False
    done = lang.answer_flip("M-0001", "haan", case_id="CS-1", event_id="E-6")
    assert done.changed and done.language == "kn-IN" and lang.current("M-0001", "hi-IN") == "kn-IN"
    assert lang.answer_flip("M-0001", "haan", case_id="CS-1", event_id="E-7").reason == "NOT_A_SWITCH"


def test_a_no_to_the_flip_is_remembered_and_not_asked_again(lang):
    lang.observe_stt("M-0001", "en-IN", 0.30, case_language="hi-IN")
    assert lang.answer_flip("M-0001", "nahi", case_id="CS-1", event_id="E-1").changed is False
    assert lang.observe_stt("M-0001", "en-IN", 0.30, case_language="hi-IN").action == "ALREADY_ASKED"
    assert lang.current("M-0001", "hi-IN") == "hi-IN"


def test_a_confident_flip_replies_in_that_language_for_one_turn_only(lang):
    r = lang.observe_stt("M-0001", "kn-IN", 0.95, case_language="hi-IN")
    assert r.action == "ADOPT_TURN" and r.reply_language == "kn-IN" and r.prompt is None
    assert lang.current("M-0001", "hi-IN") == "hi-IN"
    assert lang.observe_stt("M-0001", "hi-IN", 0.95, case_language="hi-IN").action == "SAME"
    assert lang.observe_stt("M-0001", None, None, case_language="hi-IN").action == "SAME"
    assert lang.observe_stt("M-0001", "en-IN", 0.30, case_language="hinglish").action == "SAME"     # Hinglish is Hindi + English, not a flip


def test_hindi_to_kannada_mid_case_changes_the_reply_language_and_the_voice(lang, world):
    p = VoiceTurnProcessor(FakeSarvam(transcript="ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡಿ", language="kn-IN"), world.conn, world.clock)
    r = p.process(WAV, "M-0001")
    assert r.intent == "SWITCH_LANGUAGE"
    sw = lang.apply_explicit_switch("M-0001", r.text, case_id="CS-1", event_id=r.event_id, current="hi-IN")
    assert sw.language == "kn-IN"
    _, _ = p.synthesize("ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ ಬಾಕಿ ಇದೆ.", lang.current("M-0001"))
    assert p.sarvam.tts_calls[-1][1] == "kn-IN"
