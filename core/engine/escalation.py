"""Escalation (Bible section 32): hard triggers (#52) and the soft score (#53).

Hard triggers: ANY ONE escalates immediately. ``HardTriggers.check`` is pure (no database, no clock, no model).
Soft score: ``S = 0.30 f + 0.25 u + 0.20 n + 0.15 s + 0.10 v`` from OBSERVABLE signals only. The model's own stated
confidence is never an input: ``SoftScorer.assess`` accepts the model output only to prove, in a test, that it is ignored.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_KEYWORDS = ROOT / "packs" / "paytm_merchant" / "escalation_keywords.yaml"
DEFAULT_LIMITS = ROOT / "packs" / "paytm_merchant" / "policy" / "limits.yaml"

# --------------------------------------------------------------------------------------------- trigger codes
WANTS_HUMAN = "WANTS_HUMAN"
REGULATORY_MENTION = "REGULATORY_MENTION"
FRAUD_SIGNAL = "FRAUD_SIGNAL"
NAME_MISMATCH = "NAME_MISMATCH"
AMOUNT_ABOVE_LIMIT = "AMOUNT_ABOVE_LIMIT"
SETTLEMENT_HELD_RISK = "SETTLEMENT_HELD_RISK"
OUT_OF_BAND = "OUT_OF_BAND"
GROUNDING_FAILED_TWICE = "GROUNDING_FAILED_TWICE"
FAITHFULNESS_FAILED_TWICE = "FAITHFULNESS_FAILED_TWICE"
TOOL_FAILURE = "TOOL_FAILURE"
LOOP = "LOOP"
INVALID_OUTPUT_TWICE = "INVALID_OUTPUT_TWICE"
PLAYBOOK_ESCALATE_WHEN = "PLAYBOOK_ESCALATE_WHEN"
SOFT_SCORE = "SOFT_SCORE"
BLUE_REPEATED = "BLUE_REPEATED"

TOOL_FAILURE_PREFIXES = ("CIRCUIT_OPEN", "UPSTREAM_UNAVAILABLE")
LOOP_TURNS = 3
FAILURES_FOR_ESCALATION = 2
_STRIP = dict.fromkeys(map(ord, "​‌‍⁠﻿"))   # zero-width characters
_CLAUSE_END = re.compile(r"[.,;:!?\n।|]")


@dataclass(frozen=True)
class Trigger:
    code: str
    detail: str = ""

    def as_reason(self) -> str:
        """The string the engine and the handoff packet carry as a reason."""
        return f"{self.code}: {self.detail}" if self.code == PLAYBOOK_ESCALATE_WHEN and self.detail else self.code


# --------------------------------------------------------------------------------------------- text matching
def normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").translate(_STRIP).casefold()


def _is_latin_phrase(phrase: str) -> bool:
    return all(ord(c) < 0x250 for c in phrase)


def _wordish(ch: str) -> bool:
    """Letter, mark or digit: a character that continues a word (Devanagari/Kannada vowel signs are marks)."""
    return unicodedata.category(ch)[0] in "LMN"


def find_phrase(text: str, phrase: str) -> int:
    """Index of the first acceptable occurrence of ``phrase`` in ``text`` (both already normalised), else -1.

    Decision (documented, tested): Latin phrases need a word boundary on BOTH sides (an optional plural 's'/'es' is allowed),
    so "police" does not fire inside "policeman" or "impolite"; "policy" never matches. Indic phrases need a boundary on the
    LEFT only, because case endings and plurals are glued to the stem (वकीलों, ಪೊಲೀಸರಿಗೆ).
    """
    start = 0
    latin = _is_latin_phrase(phrase)
    while True:
        i = text.find(phrase, start)
        if i < 0:
            return -1
        left_ok = i == 0 or not _wordish(text[i - 1])
        j = i + len(phrase)
        if latin:
            for suffix in ("es", "s", ""):
                if text.startswith(suffix, j) and (j + len(suffix) == len(text) or not _wordish(text[j + len(suffix)])):
                    right_ok = True
                    break
            else:
                right_ok = False
        else:
            right_ok = True
        if left_ok and right_ok:
            return i
        start = i + 1


def _tokens_around(text: str, i: int, n: int) -> list[str]:
    """Tokens within the same clause, up to n before and n after the match at ``i`` (the match itself is excluded)."""
    cs = max((m.end() for m in _CLAUSE_END.finditer(text, 0, i)), default=0)
    m = _CLAUSE_END.search(text, i)
    ce = m.start() if m else len(text)
    before = text[cs:i].split()[-n:]
    after = text[i:ce].split()[1:n + 1]
    return before + after


@dataclass(frozen=True)
class KeywordHit:
    language: str
    phrase: str
    negated: bool


class KeywordMatcher:
    def __init__(self, path: Path | str | None = None, data: Mapping | None = None):
        d = dict(data) if data is not None else yaml.safe_load(Path(path or DEFAULT_KEYWORDS).read_text(encoding="utf-8"))
        self.regulatory: dict[str, list[str]] = {lang: [normalise(p) for p in ps] for lang, ps in d["regulatory"].items()}
        self.negations: dict[str, set[str]] = {lang: {normalise(w) for w in ws} for lang, ws in (d.get("negations") or {}).items()}
        self._all_negations = set().union(*self.negations.values()) if self.negations else set()

    @property
    def languages(self) -> list[str]:
        return sorted(self.regulatory)

    def hits(self, text: str) -> list[KeywordHit]:
        t = normalise(text)
        out: list[KeywordHit] = []
        seen: set[str] = set()
        for lang, phrases in self.regulatory.items():
            for p in phrases:
                if p in seen:
                    continue
                i = find_phrase(t, p)
                if i < 0:
                    continue
                seen.add(p)
                near = {w.strip("'\"()") for w in _tokens_around(t, i, 4)}
                out.append(KeywordHit(lang, p, bool(near & self._all_negations)))
        return out


# --------------------------------------------------------------------------------------------- hard triggers
def _dig(facts: Mapping, dotted: str) -> Any:
    """facts['bank.reason'] or facts['bank']['reason']."""
    if dotted in facts:
        return facts[dotted]
    cur: Any = facts
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load_money_limit_paise(path: Path | str | None = None) -> int:
    d = yaml.safe_load(Path(path or DEFAULT_LIMITS).read_text(encoding="utf-8"))
    return int(d["money_limit"]["threshold_paise"])


class HardTriggers:
    """Any ONE trigger escalates. ``check(case, event, facts)`` returns every trigger that fires, in a fixed order.

    ``facts`` (all optional; a missing key means "no such signal"):
      text                    merchant text this turn (default: event.payload['text'])
      intent                  triaged intent (default: event.payload['intent']); 'WANTS_HUMAN' fires the human-request trigger
      wants_human             bool, an explicit flag from the triager
      fraud_flags             list/tuple of fraud signals, non-empty fires FRAUD_SIGNAL
      name_mismatch           bool, or bank.reason / 'bank' -> {'reason'} equal to 'NAME_MISMATCH'
      amount_paise            money at risk in integer paise; >= the limits.yaml threshold fires AMOUNT_ABOVE_LIMIT
      settlement_status       'HELD_RISK' fires SETTLEMENT_HELD_RISK (also read from settlement.status)
      violations              policy Violation objects; every severity 'ESCALATE' fires OUT_OF_BAND (detail = rule id)
      grounding_failures      int, consecutive failed grounding checks; >= 2 fires
      faithfulness_failures   int, same for faithfulness
      invalid_output_count    int, invalid model outputs this turn/step; >= 2 fires
      outcomes                ActionOutcome objects or error strings; an error starting CIRCUIT_OPEN / UPSTREAM_UNAVAILABLE fires
      history                 recent (intent, step) pairs INCLUDING this turn; the last 3 identical fires LOOP
      playbook_reasons        the playbook's own escalate_when expressions that are true (diagnose.escalation_reasons)
    """

    def __init__(self, *, keywords: KeywordMatcher | None = None, money_limit_paise: int | None = None,
                 limits_path: Path | str | None = None):
        self.keywords = keywords or KeywordMatcher()
        self.money_limit_paise = int(money_limit_paise) if money_limit_paise is not None else load_money_limit_paise(limits_path)

    def check(self, case: Any, event: Any, facts: Mapping | None = None) -> list[Trigger]:
        facts = facts or {}
        payload = getattr(event, "payload", None) or (event.get("payload") if isinstance(event, Mapping) else None) or {}
        out: list[Trigger] = []

        intent = facts.get("intent") or payload.get("intent")
        if intent == "WANTS_HUMAN" or facts.get("wants_human") or payload.get("wants_human"):
            out.append(Trigger(WANTS_HUMAN, "merchant asked for a person"))

        text = facts.get("text") if facts.get("text") is not None else payload.get("text")
        if text:
            hits = self.keywords.hits(str(text))
            if hits:
                detail = "; ".join(f"{h.language}:{h.phrase}" + (" (negated)" if h.negated else "") for h in hits)
                out.append(Trigger(REGULATORY_MENTION, detail))

        flags = facts.get("fraud_flags") or []
        if flags:
            out.append(Trigger(FRAUD_SIGNAL, ",".join(str(f) for f in flags)))
        if facts.get("name_mismatch") or _dig(facts, "bank.reason") == "NAME_MISMATCH":
            out.append(Trigger(NAME_MISMATCH, "bank account name does not match"))

        amount = facts.get("amount_paise")
        if amount is not None and int(amount) >= self.money_limit_paise:
            out.append(Trigger(AMOUNT_ABOVE_LIMIT, f"{int(amount)} paise >= {self.money_limit_paise}"))

        if (facts.get("settlement_status") or _dig(facts, "settlement.status")) == "HELD_RISK":
            out.append(Trigger(SETTLEMENT_HELD_RISK, "settlement held for risk review"))

        for v in facts.get("violations") or ():
            if getattr(v, "severity", None) == "ESCALATE":
                out.append(Trigger(OUT_OF_BAND, v.rule_id))

        if int(facts.get("grounding_failures") or 0) >= FAILURES_FOR_ESCALATION:
            out.append(Trigger(GROUNDING_FAILED_TWICE, f"{int(facts['grounding_failures'])} failures"))
        if int(facts.get("faithfulness_failures") or 0) >= FAILURES_FOR_ESCALATION:
            out.append(Trigger(FAITHFULNESS_FAILED_TWICE, f"{int(facts['faithfulness_failures'])} failures"))
        if int(facts.get("invalid_output_count") or 0) >= FAILURES_FOR_ESCALATION:
            out.append(Trigger(INVALID_OUTPUT_TWICE, f"{int(facts['invalid_output_count'])} invalid outputs"))

        for o in facts.get("outcomes") or ():
            err = o if isinstance(o, str) else getattr(o, "error", None)
            if err and str(err).startswith(TOOL_FAILURE_PREFIXES):
                out.append(Trigger(TOOL_FAILURE, str(err).split(":")[0]))
                break

        hist = [tuple(h) for h in (facts.get("history") or ())]
        if len(hist) >= LOOP_TURNS and len(set(hist[-LOOP_TURNS:])) == 1 and hist[-1][0] is not None:
            out.append(Trigger(LOOP, f"intent {hist[-1][0]} at step {hist[-1][1]} for {LOOP_TURNS} turns"))

        for src in facts.get("playbook_reasons") or ():
            out.append(Trigger(PLAYBOOK_ESCALATE_WHEN, str(src)))
        return out

    def as_engine_hook(self, facts_fn: Callable[[Any, Any], Mapping] | None = None) -> Callable[[Any, Any], list[str]]:
        """Adapter for ``Engine(hard_escalation=...)``: ``callable(case, event) -> list[str]`` of reasons (empty = go on).

        ``facts_fn(case, event)`` supplies the facts above (for example it calls ``diagnose.escalation_reasons`` for
        ``playbook_reasons`` and reads the system for ``amount_paise``/``settlement_status``)."""
        def hook(case: Any, event: Any) -> list[str]:
            facts = facts_fn(case, event) if facts_fn else {}
            return [t.as_reason() for t in self.check(case, event, facts)]
        return hook


# --------------------------------------------------------------------------------------------- soft score
DEFAULT_CONFIG: dict[str, float | int] = {
    "w_frustration": 0.30, "w_intent_uncertainty": 0.25, "w_novelty": 0.20, "w_speech_uncertainty": 0.15, "w_value_at_risk": 0.10,
    "escalate_at": 0.60, "blue_at": 0.45,
    "repeat_complaint_bonus": 0.20,
    "value_cap_paise": 1_000_000,          # Rs 10,000; value_at_risk = amount / this, capped at 1
    "novelty_floor": 0.35, "novelty_ceiling": 0.75,   # retrieval scores: see novelty()
}


@dataclass(frozen=True)
class ScoreConfig:
    """Weights and thresholds. Frozen (hashable); ``config_hash`` identifies it for the evaluation phase.

    [ASSUMPTION] values are the Bible's; novelty_floor/ceiling are ours and tuned on development data only."""
    w_frustration: float = 0.30
    w_intent_uncertainty: float = 0.25
    w_novelty: float = 0.20
    w_speech_uncertainty: float = 0.15
    w_value_at_risk: float = 0.10
    escalate_at: float = 0.60
    blue_at: float = 0.45
    repeat_complaint_bonus: float = 0.20
    value_cap_paise: int = 1_000_000
    novelty_floor: float = 0.35
    novelty_ceiling: float = 0.75

    def __post_init__(self) -> None:
        if not (0 <= self.blue_at <= self.escalate_at <= 1):
            raise ValueError("need 0 <= blue_at <= escalate_at <= 1")
        if self.novelty_floor >= self.novelty_ceiling:
            raise ValueError("novelty_floor must be below novelty_ceiling")
        if self.value_cap_paise <= 0:
            raise ValueError("value_cap_paise must be positive")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None = None) -> "ScoreConfig":
        merged = {**DEFAULT_CONFIG, **dict(d or {})}
        unknown = set(merged) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"unknown score config keys: {sorted(unknown)}")
        return cls(**merged)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in DEFAULT_CONFIG}

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()


def _clamp01(x: float) -> float:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        raise ValueError("signal is not a number")
    return max(0.0, min(1.0, float(x)))


# Rule-based sentiment lexicon. Only words we are sure of; a native-speaker review is expected.
ANGER_LEXICON: dict[str, list[str]] = {
    "en": ["angry", "fed up", "useless", "worst", "cheat", "waste of time", "nonsense", "pathetic", "frustrated", "harass",
           "sick of", "third time", "again and again", "ridiculous", "unacceptable"],
    "hinglish": ["bakwas", "bekar", "pareshan", "tang aa gaya", "tang aa gayi", "gussa", "dhokha", "baar baar", "kitni baar",
                 "faltu", "bewakoof"],
    "hi": ["बकवास", "बेकार", "परेशान", "गुस्सा", "धोखा", "बार बार", "फालतू"],
    "kn": ["ಕೋಪ", "ಸಿಟ್ಟು", "ಮೋಸ", "ಬೇಸರ"],
}
SENTIMENT_BASE = 0.4      # first angry phrase
SENTIMENT_STEP = 0.2      # each further distinct phrase


def rule_sentiment(text: str, language: str | None = None) -> float:
    """0..1 anger from a small lexicon. Every language list is always checked (merchants code-mix); ``language`` is accepted
    for a future per-language tuning and does not narrow the search. 0 hits -> 0.0; k hits -> min(1, 0.4 + 0.2 (k-1))."""
    t = normalise(text)
    k = sum(1 for words in ANGER_LEXICON.values() for w in words if find_phrase(t, normalise(w)) >= 0)
    return 0.0 if k == 0 else min(1.0, SENTIMENT_BASE + SENTIMENT_STEP * (k - 1))


def frustration(sentiment: float, repeated_complaint: bool, config: ScoreConfig = ScoreConfig()) -> float:
    """sentiment (0-1) plus 0.2 for a repeated complaint, capped at 1."""
    return min(1.0, _clamp01(sentiment) + (config.repeat_complaint_bonus if repeated_complaint else 0.0))


def novelty(retrieval_score: float | None, config: ScoreConfig = ScoreConfig()) -> float:
    """1 when the best retrieval score is below the floor, 0 at/above the ceiling, linear in between.

    EXPECTED SCALE (the retrieval module is written elsewhere; this is the contract we need from it): the best similarity /
    relevance of the question against the approved knowledge base, normalised to 0..1, HIGHER = better covered. ``None`` means
    no knowledge lookup was needed this turn (an action request, not a question): novelty 0."""
    if retrieval_score is None:
        return 0.0
    s = _clamp01(retrieval_score)
    if s < config.novelty_floor:
        return 1.0
    if s >= config.novelty_ceiling:
        return 0.0
    return (config.novelty_ceiling - s) / (config.novelty_ceiling - config.novelty_floor)


def speech_uncertainty(stt_confidence: float | None = None, language_probability: float | None = None,
                       language_flip: bool = False) -> float:
    """1 on a language-detection flip; else 1 - STT confidence when we have one.

    Sarvam's REST speech-to-text returns NO per-transcript confidence, only ``language_probability`` (and only when the language
    is auto-detected). With no confidence we use 1 - language_probability as a proxy: how unsure the detector is about the
    language, which is the only speech-quality signal we get. With neither (a typed message) the result is 0."""
    if language_flip:
        return 1.0
    if stt_confidence is not None:
        return 1.0 - _clamp01(stt_confidence)
    if language_probability is not None:
        return 1.0 - _clamp01(language_probability)
    return 0.0


def value_at_risk(amount_paise: int | None, config: ScoreConfig = ScoreConfig()) -> float:
    """amount / Rs 10,000, capped at 1 (money is integer paise)."""
    if not amount_paise or amount_paise < 0:
        return 0.0
    return min(1.0, int(amount_paise) / config.value_cap_paise)


WHITE, BLUE, RED = "WHITE", "BLUE", "RED"


@dataclass(frozen=True)
class SoftResult:
    score: float
    tier: str                    # WHITE | BLUE | RED
    escalate: bool               # RED for any reason: send to a human
    components: dict
    contributions: dict
    reason: str | None = None    # SOFT_SCORE | BLUE_REPEATED | UNVERIFIABLE_CLAIM | OUTSIDE_KNOWLEDGE when escalate

    @property
    def ask_clarifying_question(self) -> bool:
        return self.tier == BLUE


def soft_score(*, frustration: float, intent_uncertainty: float, novelty: float, speech_uncertainty: float, value_at_risk: float,
               config: ScoreConfig = ScoreConfig()) -> tuple[float, dict]:
    """S from the five already-computed signals (each 0..1). Returns (S rounded to 6 places, per-signal contribution)."""
    c = config
    parts = {
        "frustration": c.w_frustration * _clamp01(frustration),
        "intent_uncertainty": c.w_intent_uncertainty * _clamp01(intent_uncertainty),
        "novelty": c.w_novelty * _clamp01(novelty),
        "speech_uncertainty": c.w_speech_uncertainty * _clamp01(speech_uncertainty),
        "value_at_risk": c.w_value_at_risk * _clamp01(value_at_risk),
    }
    parts = {k: round(v, 6) for k, v in parts.items()}
    return round(sum(parts.values()), 6), parts


def tier_for(score: float, config: ScoreConfig = ScoreConfig(), *, claim_unverifiable: bool = False,
             outside_knowledge: bool = False, prior_blue_turns: int = 0) -> tuple[str, str | None]:
    """WHITE / BLUE / RED per the Bible. RED when S >= 0.60, or a claim is unverifiable / outside approved knowledge, or this is a
    second ambiguous (BLUE) turn in a row. Returns (tier, reason-when-RED)."""
    if score >= config.escalate_at:
        return RED, SOFT_SCORE
    if claim_unverifiable:
        return RED, "UNVERIFIABLE_CLAIM"
    if outside_knowledge:
        return RED, "OUTSIDE_KNOWLEDGE"
    if score >= config.blue_at:
        return (RED, BLUE_REPEATED) if prior_blue_turns >= 1 else (BLUE, None)
    return WHITE, None


class SoftScorer:
    """Turns raw observations into S. ``sentiment_fn(text, language) -> 0..1`` is an injectable model hook; when given, the
    larger of it and the rule-based sentiment is used (a model can add anger the lexicon missed, never hide it)."""

    def __init__(self, config: ScoreConfig | Mapping | None = None, sentiment_fn: Callable[[str, str | None], float] | None = None):
        self.config = config if isinstance(config, ScoreConfig) else ScoreConfig.from_dict(config)
        self.sentiment_fn = sentiment_fn

    def assess(self, *, text: str = "", language: str | None = None, repeated_complaint: bool = False,
               intent_uncertainty: float = 0.0, retrieval_score: float | None = None,
               stt_confidence: float | None = None, language_probability: float | None = None, language_flip: bool = False,
               amount_paise: int | None = None, claim_unverifiable: bool = False, outside_knowledge: bool = False,
               prior_blue_turns: int = 0, model_output: Mapping | None = None, sentiment: float | None = None) -> SoftResult:
        """``model_output`` is IGNORED on purpose (the model's stated confidence is never a trigger); it exists so a test can prove that.
        ``sentiment`` overrides the classifier when the caller already has one. ``intent_uncertainty`` comes from
        core.engine.triage.TriageResult.uncertainty."""
        del model_output
        if sentiment is None:
            sentiment = rule_sentiment(text, language)
            if self.sentiment_fn is not None:
                sentiment = max(sentiment, _clamp01(self.sentiment_fn(text, language)))
        comps = {
            "frustration": frustration(sentiment, repeated_complaint, self.config),
            "intent_uncertainty": _clamp01(intent_uncertainty),
            "novelty": novelty(retrieval_score, self.config),
            "speech_uncertainty": speech_uncertainty(stt_confidence, language_probability, language_flip),
            "value_at_risk": value_at_risk(amount_paise, self.config),
        }
        score, parts = soft_score(config=self.config, **comps)
        tier, reason = tier_for(score, self.config, claim_unverifiable=claim_unverifiable, outside_knowledge=outside_knowledge,
                                prior_blue_turns=prior_blue_turns)
        return SoftResult(score, tier, tier == RED, comps, parts, reason)
