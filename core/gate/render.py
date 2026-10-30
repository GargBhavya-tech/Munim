"""Renderer (#47): writes the merchant's reply in their language ONLY from verified claims plus fixed phrases.

The model returns a list of sentences. Each sentence is either
  * ``{"claim_ids": ["c1"], "text": "..."}``: a sentence that expresses verified claims, or
  * ``{"phrase": "thank_you"}``: a fixed conversational phrase, which code expands to the approved text (the model cannot reword it).
Mechanical limits, enforced here in code and checked again by the faithfulness check (#48):
  * every claim sentence names at least one verified claim id, and only verified ids;
  * at most one claim sentence per verified claim (so the reply cannot grow beyond its facts);
  * at most ``max_phrases`` phrases, all from the conversational list;
  * nothing else: a sentence with neither claim ids nor a phrase id is rejected, which is exactly what "it will be fixed soon" is.
The rendered reply is NEVER sent from here. It goes to the faithfulness check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.gate.phrases import Phrases
from core.gate.verify import ClaimVerdict
from core.llm.client import LLMClient, LLMResponse
from core.llm.sarvam_http import parse_json_loose

LANGUAGE_NAMES = {"en-IN": "English", "hi-IN": "Hindi in Devanagari script", "hinglish": "Hinglish (romanised Hindi mixed with English, as the merchant writes)",
                  "kn-IN": "Kannada in Kannada script"}

SYSTEM = """You write the reply a Paytm merchant will read. You are given VERIFIED CLAIMS (facts already checked against the systems) and ALLOWED PHRASES.
Reply with exactly ONE JSON object: {"sentences": [{"claim_ids": ["c1"], "text": "<sentence in the requested language>"}, {"phrase": "<allowed phrase id>"}]}
Rules:
1. Every text sentence expresses ONLY the claims named in its claim_ids, one fact per sentence, in the requested language.
2. Copy every number, amount and date exactly as the claims write them, as DIGITS (0-9) even in Hindi or Kannada, never as number words. Never change, round, convert or add one.
3. Add nothing: no promises, no timelines, no reassurance, no apology, no advice, no questions, no facts that are not in the claims.
4. Use a phrase id only from ALLOWED PHRASES, and at most the number allowed. You cannot write your own greeting or thanks; ask for the phrase id.
5. If STYLE says to mirror the merchant's mix of Hindi and English, change only the wording style, never a fact."""


class RenderError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedSentence:
    kind: str                         # claim | phrase
    text: str
    claim_ids: tuple[str, ...] = ()
    phrase_id: str | None = None


@dataclass
class RenderedReply:
    language: str
    sentences: list[RenderedSentence]
    llm: LLMResponse | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)

    @property
    def claim_sentences(self) -> list[RenderedSentence]:
        return [s for s in self.sentences if s.kind == "claim"]


class Renderer:
    def __init__(self, llm: LLMClient, phrases: Phrases | None = None, *, max_phrases: int = 3):
        self.llm, self.phrases, self.max_phrases = llm, phrases or Phrases(), max_phrases

    def render(self, verified: list[ClaimVerdict], language: str, *, style_hint: str | None = None, feedback: str | None = None,
               case_id: str | None = None) -> RenderedReply:
        claims = [v for v in verified if v.sendable]
        payload = {"language": LANGUAGE_NAMES.get(language, "English"), "claims": [{"id": v.claim_id, "text": v.text} for v in claims],
                   "allowed_phrases": list(self.phrases.ids("conversational")), "max_phrases": self.max_phrases}
        if style_hint:
            payload["style"] = style_hint
        if feedback:
            payload["feedback"] = feedback
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        last: Exception | None = None
        for _ in range(2):
            r = self.llm.complete(msgs, case_id=case_id, purpose="render", response_format={"type": "json_object"})
            try:
                return RenderedReply(language, self._parse(r.text, claims, language), r)
            except RenderError as e:
                last = e
                msgs = msgs[:2] + [{"role": "user", "content": f"INVALID_RENDER: {e}. Reply again with one valid JSON object that follows the rules."}]
        raise RenderError(f"renderer output invalid twice: {last}")

    def _parse(self, text: str, claims: list[ClaimVerdict], language: str) -> list[RenderedSentence]:
        try:
            obj = parse_json_loose(text)
        except ValueError:
            raise RenderError("not valid JSON") from None
        items = obj.get("sentences") if isinstance(obj, dict) else None
        if not isinstance(items, list) or not items or set(obj) != {"sentences"}:
            raise RenderError("expected {\"sentences\": [...]} and nothing else")
        ids = {v.claim_id for v in claims}
        out: list[RenderedSentence] = []
        phrases = 0
        for n, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise RenderError(f"sentence {n} is not an object")
            if set(it) == {"phrase"}:
                pid = it["phrase"]
                if not isinstance(pid, str) or not self.phrases.has(pid) or self.phrases.kind(pid) != "conversational":
                    raise RenderError(f"sentence {n}: phrase id not allowed")
                phrases += 1
                out.append(RenderedSentence("phrase", self.phrases.text(pid, language), (), pid))
            elif set(it) == {"claim_ids", "text"}:
                cids, txt = it["claim_ids"], it["text"]
                if not isinstance(cids, list) or not cids or not all(isinstance(c, str) for c in cids) or not isinstance(txt, str) or not txt.strip():
                    raise RenderError(f"sentence {n}: needs claim_ids and text")
                if any(c not in ids for c in cids):
                    raise RenderError(f"sentence {n}: cites a claim that is not verified")
                out.append(RenderedSentence("claim", txt.strip(), tuple(cids)))
            else:
                raise RenderError(f"sentence {n}: must be a claim sentence or a phrase")
        if phrases > self.max_phrases:
            raise RenderError(f"at most {self.max_phrases} phrases")
        if len([s for s in out if s.kind == "claim"]) > len(claims):
            raise RenderError("more sentences than verified claims")
        return out
