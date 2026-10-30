"""Faithfulness check (#48): the rendered reply must say nothing the verified claims do not say.

For each CLAIM sentence of the reply (fixed phrases are exempt, they are approved text):
  1. numbers on the rendered text: every number, amount and date, in digits or in Hindi/Kannada words, must be in a claim the
     sentence cites (core.gate.numbers handles Indic digits and number words);
  2. back-translate the sentence to English (Sarvam translate, split at the 1,000-character limit) and check the numbers again there;
  3. ENTAILMENT in English: the premise is a verified claim (never the other way round), the hypothesis is the back-translated sentence.
     At least one verified claim, or the cited claims joined, must entail it. The renderer's own citation is not trusted;
  4. when an Indic-capable embedder is given: the sentence must sit close enough to the claim that entails it (a second, independent signal).
English replies skip the translation and are checked directly. A sentence that fails any step fails the reply.
The translator and NLI model are injected so tests do not need the network; the real false-block rate is measured on labelled replies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np

from core.gate.nli import ENTAILMENT
from core.gate.numbers import extract_numbers, number_key
from core.gate.render import RenderedReply
from core.gate.verify import ClaimVerdict

TRANSLATE_LIMIT = 1000
CHUNK = 900
_SENT = re.compile(r"(?<=[.!?।])\s+")
_SARVAM_CODE = {"hi-IN": "hi-IN", "kn-IN": "kn-IN", "en-IN": "en-IN", "hinglish": "auto"}


class Translator(Protocol):
    def to_english(self, text: str, language: str) -> str: ...


class SarvamTranslator:
    """mayura:v1 through core.llm.sarvam_http. Text over the 1,000-character limit is split on sentence ends and joined back."""

    def __init__(self, sarvam):
        self.sarvam = sarvam

    @staticmethod
    def split(text: str, limit: int = CHUNK) -> list[str]:
        pieces: list[str] = []
        for s in _SENT.split(text.strip()):
            while len(s) > limit:                      # one sentence longer than the limit: cut on a space if there is one
                cut = s.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                pieces.append(s[:cut].strip())
                s = s[cut:].strip()
            if s:
                pieces.append(s)
        chunks, cur = [], ""
        for p in pieces:
            if cur and len(cur) + 1 + len(p) > limit:
                chunks.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}".strip()
        if cur:
            chunks.append(cur)
        return chunks

    def to_english(self, text: str, language: str) -> str:
        src = _SARVAM_CODE.get(language, "auto")
        return " ".join(self.sarvam.translate(c, src, "en-IN")["translated_text"] for c in self.split(text))


@dataclass(frozen=True)
class SentenceCheck:
    index: int
    text: str
    kind: str
    ok: bool
    reasons: tuple[str, ...] = ()
    back_translation: str | None = None
    supported_by: str | None = None            # id of the claim (or "joined") that entails it
    nli_label: str | None = None
    similarity: float | None = None


@dataclass
class FaithResult:
    ok: bool
    sentences: list[SentenceCheck] = field(default_factory=list)

    @property
    def failed(self) -> list[SentenceCheck]:
        return [s for s in self.sentences if not s.ok]


def _keys(text: str) -> set:
    return {number_key(n) for n in extract_numbers(text, english_one=True)}


class FaithfulnessChecker:
    def __init__(self, nli, translator: Translator, embedder=None, *, similarity_floor: float = 0.5):
        self.nli, self.translator, self.embedder, self.floor = nli, translator, embedder, similarity_floor

    def check(self, reply: RenderedReply, verdicts: Sequence[ClaimVerdict]) -> FaithResult:
        claims = {v.claim_id: v for v in verdicts if v.sendable}
        results: dict[int, dict] = {}
        pending: list[tuple[int, str, str, str]] = []              # (sentence index, premise id, premise text, hypothesis)
        for i, s in enumerate(reply.sentences):
            if s.kind != "claim":
                continue
            r = {"reasons": [], "back": None, "numbers_ok": True}
            results[i] = r
            cited = [claims[c] for c in s.claim_ids if c in claims]
            if len(cited) != len(s.claim_ids):
                r["reasons"].append("cites_unverified_claim")
                continue
            allowed = set().union(*[_keys(c.text) for c in cited]) if cited else set()
            extra = [n.raw for n in extract_numbers(s.text, english_one=True) if number_key(n) not in allowed]
            if extra:
                r["reasons"].append("number_not_in_cited_claims: " + ", ".join(extra))
                continue
            en = s.text if reply.language == "en-IN" else self.translator.to_english(s.text, reply.language)
            r["back"] = en
            extra_en = [n.raw for n in extract_numbers(en, english_one=True) if number_key(n) not in allowed]
            if extra_en:
                r["reasons"].append("number_not_in_cited_claims_after_back_translation: " + ", ".join(extra_en))
                continue
            for cid, c in claims.items():
                pending.append((i, cid, c.text, en))
            if len(cited) > 1:
                pending.append((i, "joined", " ".join(c.text for c in cited), en))

        labels = self.nli.label_batch([(p, h) for _, _, p, h in pending]) if pending else []
        for (i, pid, ptext, hyp), (lab, _) in zip(pending, labels):
            r = results[i]
            if lab == ENTAILMENT and "supported_by" not in r:
                r["supported_by"], r["premise"] = pid, ptext
            r.setdefault("label", lab)
            if lab == ENTAILMENT:
                r["label"] = ENTAILMENT

        out: list[SentenceCheck] = []
        for i, s in enumerate(reply.sentences):
            if s.kind != "claim":
                out.append(SentenceCheck(i, s.text, s.kind, True))
                continue
            r = results[i]
            reasons = list(r["reasons"])
            sim = None
            if not reasons:
                if "supported_by" not in r:
                    reasons.append(f"not_entailed_by_any_verified_claim ({r.get('label', 'NEUTRAL')})")
                elif self.embedder is not None and reply.language != "en-IN":
                    v = self.embedder.encode([s.text, r["premise"]], kind="passage")
                    sim = float(np.dot(v[0], v[1]))
                    if sim < self.floor:
                        reasons.append(f"low_similarity ({sim:.2f} < {self.floor})")
            out.append(SentenceCheck(i, s.text, s.kind, not reasons, tuple(reasons), r["back"], r.get("supported_by"), r.get("label"), sim))
        return FaithResult(all(c.ok for c in out), out)
