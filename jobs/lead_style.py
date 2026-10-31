"""The style of the first contact: a voice note, a data-led email, a rep, or a human. It changes with the person.

``jobs/approach.py`` says HOW MUCH of a person the first contact should be. This module says what it LOOKS like, built entirely by code from the lead's brief and the approved fixed
phrases, so nothing a model wrote can reach a lead:

  * A SHOP that gets a voice call level (the middle of the ladder) gets a VOICE NOTE: a script of about twenty seconds, one idea, plain words. The idea is the main pitch sentence
    from the brief (an approved knowledge-base quote); a sentence with trade jargon (MDR and the like) is skipped for the next one, and a script over the word budget loses its
    greeting first. Audio is made only if a text-to-speech function is given; otherwise the script is returned and the plan says no audio was made.
  * An ONLINE BRAND (D2C) gets a DATA-LED EMAIL: the brief's own sourced facts, each with its source, plus the identity and PIN/OTP disclosures. At the top level it gets a human call.
  * A SHOP at the top level gets a REP VISIT; nothing is sent. The lowest level gets the approved first contact only, and the self-serve link goes after they reply (a first contact
    never carries a link).
  * A BANK or institution gets a HUMAN account manager. Nothing is sent by the AI at all; it only does the homework (the brief).

Language: the lead's language is used if it is known. Where it is not, the plan says so and the note is English: a language is never guessed from a region. A non-English idea
sentence needs a translator; without one it stays English and the plan says so. A translation must still pass the grounding gate's faithfulness check before it is sent, and that
check belongs to the caller, so ``translated`` is returned and nothing is sent from here. Pure code: no clock, no model.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from core.gate.phrases import Phrases
from jobs.approach import D2C_TOP, HUMAN, Approach
from jobs.briefs import Brief
from jobs.outreach import segment_for

JARGON = ("mdr", "tat", "t+1", "interchange", "acquirer", "payment gateway", "settlement cycle")
MAX_VOICE_WORDS = 45                      # [ASSUMPTION] about twenty seconds at a plain speaking pace (roughly 2.25 words a second)
WORDS_PER_SECOND = 2.25
_JARGON = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(j) for j in JARGON) + r")(?![a-z0-9])", re.I)


@dataclass(frozen=True)
class VoiceNote:
    language: str
    script: str
    words: int
    seconds_estimate: int
    translated: bool
    audio_made: bool
    audio: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    lines: tuple[dict, ...]
    disclosure: str


@dataclass(frozen=True)
class StylePlan:
    kind: str                     # human_only | rep_visit | voice_note | link_after_reply | data_email | human_call
    owner: str
    language: str
    why: tuple[str, ...]
    steps: tuple[str, ...]
    voice_note: VoiceNote | None = None
    email: EmailDraft | None = None
    gaps: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        d = asdict(self)
        if d.get("voice_note"):
            d["voice_note"].pop("audio", None)
        return json.loads(json.dumps(d))                                    # plain JSON data: tuples become lists, so what is returned equals what is sent


def has_jargon(text: str) -> bool:
    return bool(_JARGON.search(text))


def plain_idea(brief: Brief) -> tuple[str | None, list[str]]:
    """The first pitch sentence with no trade jargon, and a note for each one skipped."""
    skipped = []
    for p in brief.pitch:
        if has_jargon(p[2].text):
            skipped.append(f"Skipped the {p[1]!r} sentence: it uses trade jargon")
            continue
        return p[2].text, skipped
    return None, skipped


def build_voice_note(brief: Brief, language: str | None, phrases: Phrases, *, translate: Callable[[str, str], str] | None = None,
                     tts: Callable[[str, str], bytes] | None = None) -> tuple[VoiceNote | None, list[str]]:
    gaps: list[str] = []
    lang = language or "en-IN"
    if language is None:
        gaps.append("The lead's language is not on record: ask which language they prefer. This note is English; a language is never guessed from the area")
    idea, skipped = plain_idea(brief)
    gaps += skipped
    if idea is None:
        return None, gaps + ["Every approved pitch sentence uses trade jargon, so there is no plain sentence to say"]
    translated = False
    if lang != "en-IN":
        if translate is None:
            gaps.append(f"No translator is available: the offer sentence stays in English inside a {lang} note")
        else:
            idea, translated = translate(idea, lang), True
            gaps.append("The translated sentence must pass the faithfulness check before it is sent")
    greeting, identity = phrases.text("greeting", lang), phrases.text("identity", lang)
    parts = [greeting, identity, idea]
    if sum(len(p.split()) for p in parts) > MAX_VOICE_WORDS:
        parts = [identity, idea]                                         # the greeting goes first
    script = " ".join(parts)
    words = len(script.split())
    if words > MAX_VOICE_WORDS:
        gaps.append(f"The approved sentence is too long for twenty seconds ({words} words); a person must shorten it from the same source")
    audio = tts(script, lang) if tts else None
    if tts is None:
        gaps.append("No text-to-speech is connected, so only the script was made")
    return VoiceNote(lang, script, words, math.ceil(words / WORDS_PER_SECOND), translated, audio is not None, audio), gaps


def build_email(row, brief: Brief, phrases: Phrases) -> EmailDraft:
    lines = [{"text": c.text, "source": c.source} for c in brief.who]
    if brief.pitch:
        lines.append({"text": brief.pitch[0][2].text, "source": brief.pitch[0][2].source})
    return EmailDraft(f"A short note on payments for {row['name']}", tuple(lines), f"{phrases.text('identity', 'en-IN')} {phrases.text('pin_otp', 'en-IN')}")


def plan_style(row, brief: Brief, approach: Approach, *, language: str | None = None, phrases: Phrases | None = None,
               translate: Callable[[str, str], str] | None = None, tts: Callable[[str, str], bytes] | None = None) -> StylePlan:
    phrases = phrases or Phrases()
    seg = segment_for(row["category"])
    lang = language or "en-IN"
    if approach.level == HUMAN:
        return StylePlan("human_only", "a human account manager", lang, approach.reasons,
                         ("The AI prepares the brief and nothing else", "A person makes the first contact and owns the relationship"))
    if seg == "d2c":
        if approach.level == D2C_TOP:
            return StylePlan("human_call", "a human, by phone", lang, approach.reasons,
                             ("A person calls the founder with the brief in hand", "The data-led email follows the call, not before it"), email=build_email(row, brief, phrases))
        return StylePlan("data_email", "Munim, by email", "en-IN", approach.reasons,
                         ("One email built from the brief's own sourced facts", "No link, and the identity and PIN/OTP disclosures come first"), email=build_email(row, brief, phrases))
    if approach.level == "field_rep":
        return StylePlan("rep_visit", "a field rep", lang, approach.reasons, ("Nothing is sent first", "A rep visits on a planned route, with a briefing that fits their experience"))
    if approach.level == "ai_voice":
        note, gaps = build_voice_note(brief, language, phrases, translate=translate, tts=tts)
        return StylePlan("voice_note" if note else "link_after_reply", "Munim, by voice note", lang, approach.reasons,
                         ("One voice note of about twenty seconds, one idea, plain words", "Sent only on a channel the lead has consented to"), voice_note=note, gaps=tuple(gaps))
    return StylePlan("link_after_reply", "Munim, by the approved first message", lang, approach.reasons,
                     ("The first message is the approved identity and PIN/OTP text, with no link", "The self-serve link is sent only after they reply"))
