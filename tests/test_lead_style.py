"""The style of a first contact: a short plain voice note, a data-led email, a rep, or a human. Built from the brief and approved phrases, never from a model."""
import math

import pytest

from core.gate.phrases import Phrases
from core.gate.sources import Claim
from jobs.approach import D2C_TOP, HUMAN, Approach
from jobs.briefs import AccountBriefJob
from jobs.dedupe import DedupeJob
from jobs.lead_style import MAX_VOICE_WORDS, WORDS_PER_SECOND, build_email, build_voice_note, has_jargon, plan_style, plain_idea
from jobs.scoring import score_ready_leads
from tests.checkpoint_script import SCRIPT
from tests.world import World

PH = Phrases()


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    DedupeJob(w.conn, w.clock).run()
    score_ready_leads(w.conn, w.clock)
    return w


def lead(w, category):
    r = w.conn.execute("SELECT * FROM leads WHERE state = 'NEW' AND matched_merchant_id IS NULL AND category = ? AND score IS NOT NULL ORDER BY score DESC LIMIT 1", (category,)).fetchone()
    return r, AccountBriefJob(w.conn, w.clock).build(r["lead_id"])


def A(level, bumped=False):
    return Approach(level, level, bumped, ("because",))


@pytest.mark.parametrize("text,yes", [("an MDR of some kind", True), ("the TAT is short", True), ("T+1 settlement", True), ("mdr", True), ("a kindergarten", False), ("rupees per payment", False)])
def test_trade_jargon_is_found_as_a_word_and_not_inside_other_words(text, yes):
    assert has_jargon(text) is yes


def test_the_voice_note_is_one_idea_in_plain_words_inside_the_twenty_second_budget(world):
    row, brief = lead(world, "kirana")
    note, gaps = build_voice_note(brief, "en-IN", PH)
    assert note.words <= MAX_VOICE_WORDS and note.seconds_estimate == math.ceil(note.words / WORDS_PER_SECOND) and not has_jargon(note.script)
    assert PH.text("identity", "en-IN") in note.script and plain_idea(brief)[0] in note.script
    assert any("No text-to-speech" in g for g in gaps) and note.audio_made is False


def test_an_unknown_language_is_never_guessed_and_the_plan_says_so(world):
    _, brief = lead(world, "kirana")
    note, gaps = build_voice_note(brief, None, PH)
    assert note.language == "en-IN" and any("not on record" in g and "never guessed" in g for g in gaps)


def test_a_non_english_note_without_a_translator_keeps_the_sentence_in_english_and_says_so(world):
    _, brief = lead(world, "kirana")
    note, gaps = build_voice_note(brief, "kn-IN", PH)
    assert PH.text("identity", "kn-IN") in note.script and note.translated is False and any("No translator" in g for g in gaps)


def test_a_translator_is_used_and_the_faithfulness_check_is_flagged_for_the_caller(world):
    _, brief = lead(world, "kirana")
    note, gaps = build_voice_note(brief, "hi-IN", PH, translate=lambda t, l: f"[{l}] {t}")
    assert note.translated is True and "[hi-IN]" in note.script and any("faithfulness" in g for g in gaps)


def test_audio_is_made_only_when_a_speaker_is_given(world):
    _, brief = lead(world, "kirana")
    calls = []
    note, _ = build_voice_note(brief, "en-IN", PH, tts=lambda s, l: calls.append((s, l)) or b"wav")
    assert note.audio_made is True and calls == [(note.script, "en-IN")]


def test_a_greeting_is_dropped_first_when_the_note_would_run_long(world):
    _, brief = lead(world, "kirana")
    long_idea = " ".join(["word"] * 40)
    brief.pitch[0] = (brief.pitch[0][0], brief.pitch[0][1], Claim("p0", long_idea + ".", brief.pitch[0][2].source))
    note, _ = build_voice_note(brief, "en-IN", PH)
    assert PH.text("greeting", "en-IN") not in note.script and PH.text("identity", "en-IN") in note.script


def test_a_pitch_sentence_with_jargon_is_skipped_for_the_next_plain_one(world):
    _, brief = lead(world, "kirana")
    c0 = brief.pitch[0][2]
    brief.pitch[0] = (brief.pitch[0][0], brief.pitch[0][1], Claim(c0.id, "The MDR is low.", c0.source))
    idea, skipped = plain_idea(brief)
    assert idea == brief.pitch[1][2].text and len(skipped) == 1 and "jargon" in skipped[0]


def test_if_every_sentence_has_jargon_there_is_no_note_and_the_reason_is_given(world):
    _, brief = lead(world, "kirana")
    brief.pitch[:] = [(r, l, Claim(c.id, "The MDR applies.", c.source)) for r, l, c in brief.pitch]
    note, gaps = build_voice_note(brief, "en-IN", PH)
    assert note is None and any("trade jargon" in g for g in gaps)


def test_an_online_brand_gets_a_data_led_email_from_the_briefs_sourced_facts_with_the_disclosures(world):
    row, brief = lead(world, "d2c")
    p = plan_style(row, brief, A("ai_voice"))
    assert p.kind == "data_email" and p.voice_note is None
    assert {(l["text"], l["source"]) for l in p.email.lines} >= {(c.text, c.source) for c in brief.who}
    assert PH.text("identity", "en-IN") in p.email.disclosure and PH.text("pin_otp", "en-IN") in p.email.disclosure
    assert "http" not in p.email.disclosure and all("http" not in l["text"] for l in p.email.lines)


def test_an_online_brand_at_the_top_level_gets_a_human_call_and_the_email_comes_after(world):
    row, brief = lead(world, "d2c")
    p = plan_style(row, brief, A(D2C_TOP))
    assert p.kind == "human_call" and any("follows the call" in s for s in p.steps) and p.email


def test_a_bank_gets_a_human_and_nothing_is_sent_by_the_ai(world):
    row, brief = lead(world, "kirana")
    p = plan_style(row, brief, A(HUMAN))
    assert p.kind == "human_only" and p.voice_note is None and p.email is None and "human" in p.owner


def test_a_shop_in_the_middle_gets_a_voice_note_a_top_shop_a_rep_and_the_bottom_a_link_after_a_reply(world):
    row, brief = lead(world, "kirana")
    assert plan_style(row, brief, A("ai_voice")).kind == "voice_note"
    assert plan_style(row, brief, A("field_rep")).kind == "rep_visit"
    low = plan_style(row, brief, A("self_serve"))
    assert low.kind == "link_after_reply" and any("no link" in s for s in low.steps)


def test_the_plan_is_plain_data_without_the_audio_bytes(world):
    import json
    row, brief = lead(world, "kirana")
    d = plan_style(row, brief, A("ai_voice"), tts=lambda s, l: b"wav").as_dict()
    assert json.loads(json.dumps(d)) == d and "audio" not in d["voice_note"] and d["voice_note"]["audio_made"] is True
