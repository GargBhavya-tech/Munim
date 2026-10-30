"""RFP drafting and the buyer-intent feed (Build Map #120; Bible: enterprise side, SIMULATED).

``draft_rfp_response`` answers each question of a synthetic RFP ONLY from approved KB passages and system facts handed in by the caller.
The answer text is never free text: it is built from claims that passed ``GroundingVerifier`` (source exists, numbers equal the source's
numbers, source entails the claim). A claim that fails is dropped and listed with its reason. A question with no verified claim is marked
NEEDS_HUMAN and carries no answer text. Numbers come only from the cited source (system facts are read-only inputs; nothing is computed).
``claim_writer`` is the seam for a model: it receives (question, candidate passages) and returns Claims; the default is extractive
(one claim per sentence of the best passages), so an offline run needs no model.

``detect_intents`` lists buyer intents found in synthetic inbound texts with plain keyword rules (deterministic, no model).
Everything produced here is labelled SIMULATED.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from core.gate.sources import Claim, SourceBook
from core.gate.verify import ClaimVerdict, GroundingVerifier
from core.memory.knowledge import KBDoc, load_kb_dir

LABEL = "SIMULATED"
KB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "kb"
_WORD = re.compile(r"[a-z0-9]+")
_SENT = re.compile(r"(?<=[.!?])\s+")
_STOP = {"the", "a", "an", "is", "are", "of", "to", "for", "in", "on", "and", "or", "it", "this", "that", "be", "by", "with", "how", "what", "do",
         "does", "can", "you", "your", "we", "our", "will", "please", "describe", "explain", "state", "provide", "i", "my"}
_Q = re.compile(r"^\s*(?:Q\s*)?(\d+)[.):]\s*(.+?)\s*$", re.I)

ClaimWriter = Callable[[str, "list[Passage]"], "list[Claim]"]


@dataclass(frozen=True)
class Passage:
    source_id: str            # kb:<doc>#<section>@v<n>
    doc_id: str
    review_status: str
    text: str


@dataclass
class RfpAnswer:
    qid: str
    question: str
    status: str                                   # ANSWERED | NEEDS_HUMAN
    claims: list[Claim] = field(default_factory=list)       # verified, in order
    text: str = ""
    dropped: list[dict] = field(default_factory=list)       # {claim_id, source, reason}
    citations: list[str] = field(default_factory=list)


@dataclass
class RfpResponse:
    answers: list[RfpAnswer]
    label: str = LABEL

    def render(self) -> str:
        out = [f"[{self.label}] RFP response (draft for human review)"]
        for a in self.answers:
            out.append(f"\n{a.qid}. {a.question}")
            if a.status == "ANSWERED":
                out.append(a.text + "  " + " ".join(f"[{c}]" for c in a.citations))
            else:
                out.append("NEEDS_HUMAN: no verified source for this question.")
        return "\n".join(out)


def parse_questions(rfp_text: str) -> list[tuple[str, str]]:
    """Numbered questions ("Q1. ...", "2) ...") in order. Other lines are ignored."""
    out = []
    for line in rfp_text.splitlines():
        m = _Q.match(line)
        if m:
            out.append((f"Q{m.group(1)}", m.group(2)))
    return out


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 2}


def kb_passages(kb_dir: Path = KB_DIR, *, require_reviewed: bool = False) -> list[Passage]:
    docs: list[KBDoc] = load_kb_dir(kb_dir)
    return [Passage("kb:" + d.chunk_id(s.section_id), d.doc_id, d.review_status, s.text)
            for d in docs if not require_reviewed or d.review_status == "REVIEWED" for s in d.sections]


def retrieve(question: str, passages: Sequence[Passage], k: int = 2, min_overlap: int = 2) -> list[Passage]:
    q = _tokens(question)
    scored = sorted(((len(q & _tokens(p.text)), p.source_id, p) for p in passages), key=lambda t: (-t[0], t[1]))
    return [p for n, _, p in scored[:k] if n >= min_overlap]


def extractive_writer(qid: str) -> ClaimWriter:
    def write(question: str, cands: list[Passage]) -> list[Claim]:
        out, n = [], 0
        for p in cands:
            for s in (x.strip() for x in _SENT.split(p.text.strip())):
                if s:
                    n += 1
                    out.append(Claim(f"{qid}.c{n}", s, p.source_id))
        return out
    return write


def draft_rfp_response(rfp_text: str, nli, *, passages: Sequence[Passage] | None = None, system_facts: Mapping[str, dict] | None = None,
                       claim_writer: ClaimWriter | None = None) -> RfpResponse:
    """``system_facts``: {"tool:<name>#<call>": {...}} read-only system reads; they join the SourceBook and can be cited."""
    passages = list(passages) if passages is not None else kb_passages()
    verifier = GroundingVerifier(nli)
    answers: list[RfpAnswer] = []
    for qid, question in parse_questions(rfp_text):
        cands = retrieve(question, passages)
        book = SourceBook()
        for p in cands:
            book.add_kb(p.source_id, p.text)
        for sid, body in (system_facts or {}).items():
            book.add_tool_result(sid, dict(body))
        claims = (claim_writer or extractive_writer(qid))(question, cands) if (cands or system_facts) else []
        verdicts: list[ClaimVerdict] = verifier.verify(list(claims), book) if claims else []
        ok = [v for v in verdicts if v.sendable]
        by_id = {c.id: c for c in claims}
        kept = [by_id[v.claim_id] for v in ok]
        dropped = [{"claim_id": v.claim_id, "source": v.source, "reason": v.reason} for v in verdicts if not v.sendable]
        if kept:
            a = RfpAnswer(qid, question, "ANSWERED", kept, " ".join(c.text for c in kept), dropped, sorted({c.source for c in kept}))
        else:
            a = RfpAnswer(qid, question, "NEEDS_HUMAN", [], "", dropped, [])
        answers.append(a)
    return RfpResponse(answers)


# ------------------------------------------------------------------------------------------------ intent feed
INTENT_RULES: dict[str, tuple[str, ...]] = {
    "PRICING_ENQUIRY": ("price", "pricing", "cost", "fee", "quote", "rate card"),
    "INTEGRATION_ENQUIRY": ("api", "integrate", "integration", "sdk", "webhook", "erp"),
    "SECURITY_REVIEW": ("security", "audit", "compliance", "data residency", "certification"),
    "VOLUME_DEPLOYMENT": ("outlets", "stores", "branches", "devices", "rollout", "franchise"),
    "TENDER_RFP": ("rfp", "tender", "proposal", "bid"),
    "DEMO_REQUEST": ("demo", "pilot", "trial", "walkthrough"),
}


def detect_intents(inputs: Iterable[Mapping]) -> dict:
    """``inputs``: synthetic {"id", "buyer", "text"}. A feed row per (input, intent) with the matched keywords. No model, no numbers."""
    feed = []
    for item in inputs:
        low = str(item.get("text", "")).lower()
        for intent, kws in INTENT_RULES.items():
            hit = [k for k in kws if re.search(r"\b" + re.escape(k) + r"\b", low)]
            if hit:
                feed.append({"input_id": item.get("id"), "buyer": item.get("buyer"), "intent": intent, "matched": hit})
    return {"label": LABEL, "feed": feed}
