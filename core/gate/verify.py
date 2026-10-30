"""Grounding steps 1-4 (Bible section 29): source check, numbers check, English entailment, corroboration level.

``verbalise`` turns a source into plain sentences first. The entailment model reads sentences, not status codes: bare values like
"status: OFFLINE" fail it (measured in #45), so every tool result is rendered as "Device status is OFFLINE." before it is a premise.

``GroundingVerifier.verify`` gives every claim a verdict. A claim is sendable only when ALL of these hold:
  1. the cited source exists in this turn's SourceBook;
  2. every number in the claim equals a number in THAT source (core.gate.sources.check_claim);
  3. the cited source ENTAILS the claim (premise = the source, hypothesis = the claim; nli.py owns the order);
  4. nothing contradicts it and no policy rule forbids it.
Other sources of a different kind can only RAISE a verified claim from SUPPORTED to CORROBORATED. They can never rescue a claim whose cited
source does not entail it, and they can never block one: the entailment model misreads unrelated text as a contradiction (measured in #51,
an unrelated loan-rate policy "contradicted" device and fee claims), so a policy rule blocks a claim only through ``forbid_check``, the
deterministic rule engine, which reports the rule id. Levels are categorical and come from corroboration.level.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from core.gate.corroboration import Evidence, Level, level
from core.gate.nli import CONTRADICTION, ENTAILMENT
from core.gate.sources import Claim, SourceBook, check_claim

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_WINDOW = re.compile(r"(?<![a-z0-9])(\d+)([dhm])(?![a-z0-9])")
_WINDOW_UNITS = {"d": "days", "h": "hours", "m": "minutes"}
_WORD = re.compile(r"[a-z]+")
# Words that say nothing about WHAT a sentence is about. Two sentences sharing only these are unrelated.
_STOP = frozenset("""a an the and or but not no is are was were be been being has have had do does did will would can could shall should may
might must to of in on at by for from with as it its this that these those there here your you yours i me my we our us they them their he she
his her item value id type name status only right now just also very more most than then so if when""".split())


def _label(key: str) -> str:
    k = _CAMEL.sub("_", key).lower()
    for suffix in ("_paise", "_at", "_id"):
        if k.endswith(suffix) and len(k) > len(suffix):
            k = k[: -len(suffix)] + ({"_at": " time", "_id": " id"}.get(suffix, ""))
    k = _WINDOW.sub(lambda m: f"{m.group(1)} {_WINDOW_UNITS[m.group(2)]}", k)    # replacements_last_30d -> replacements last 30 days
    return k.replace("_", " ").strip()


def _fmt_value(key: str, v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float, Decimal)) and key.lower().endswith("_paise"):
        d = Decimal(str(v)) / 100
        return (f"{d:f}".rstrip("0").rstrip(".") if d % 1 else f"{int(d)}") + " rupees"
    if isinstance(v, str) and _CLOCK.match(v):
        h, mi = int(v[:2]), int(v[3:5])
        return f"{v} ({h % 12 or 12}{'' if mi == 0 else f':{mi:02d}'} {'am' if h < 12 else 'pm'})"
    if isinstance(v, str) and _ISO.match(v):
        try:
            t = datetime.fromisoformat(v)
            return f"{t.day} {t:%B %Y} at {t:%H:%M} ({v})"
        except ValueError:
            return v
    return str(v)


def _walk(v: Any, prefix: str, key: str, out: list[str]) -> None:
    if v is None:
        return
    if isinstance(v, Mapping):
        for k, x in v.items():
            if isinstance(x, (Mapping, list, tuple)):
                _walk(x, f"{prefix} {_label(str(k))}".strip(), str(k), out)
            elif x is not None:
                out.append(f"{(prefix + ' ' + _label(str(k))).strip().capitalize()} is {_fmt_value(str(k), x)}.")
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v[:20], 1):
            if isinstance(x, (Mapping, list, tuple)):
                _walk(x, f"{prefix} item {i}".strip(), key, out)
            elif x is not None:
                out.append(f"{prefix.capitalize()} item {i} is {_fmt_value(key, x)}.")
    else:
        out.append(f"{prefix.capitalize() or 'Value'} is {_fmt_value(key, v)}.")


def verbalise(source_id: str, body: Any) -> str:
    """A source as plain English sentences for the entailment model. Text stays as it is; structured results become sentences."""
    if isinstance(body, str):
        return body
    name = source_id.split(":", 1)[-1].split("#", 1)[0]
    subject = re.sub(r"^get_|_status$|^check_", "", name).replace("_", " ").strip()
    out: list[str] = []
    _walk(body, subject, "", out)
    return " ".join(out)


def _content(text: str, aliases: Mapping[str, str]) -> set[str]:
    words = (aliases.get(w, w) for w in _WORD.findall(text.lower()))
    return {w[:5] for w in words if len(w) > 2 and w not in _STOP}


def about(sentence: str, claim: str, aliases: Mapping[str, str] | None = None) -> bool:
    """True when ``sentence`` shares a content word with ``claim`` (5-letter stems; pack aliases applied, e.g. soundbox -> device).
    A lone sentence of a source may count as entailment only when it is about the claim: on a tiny unrelated premise ("Merchant city is
    Mysuru.") the entailment model says ENTAILMENT for "Your KYC is approved." (measured, BUILD_PLAN A1)."""
    a = aliases or {}
    return bool(_content(sentence, a) & _content(claim, a))


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
MAX_SENTENCES = 12


def _premises(text: str) -> list[str]:
    """The whole source first, then its sentences (when there is more than one)."""
    parts = [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()][:MAX_SENTENCES]
    return [text] + parts if len(parts) > 1 else [text]


def source_kind(source_id: str) -> str:
    return source_id.split(":", 1)[0] if source_id.split(":", 1)[0] in ("tool", "kb", "policy") else "tool"


@dataclass(frozen=True)
class ClaimVerdict:
    claim_id: str
    text: str
    source: str
    level: str
    sendable: bool
    color: str
    reason: str | None = None
    rule_id: str | None = None
    source_exists: bool = False
    numbers_ok: bool = False
    nli_label: str | None = None
    nli_probs: dict = field(default_factory=dict)
    corroborated_by: tuple[str, ...] = ()

    def as_chip(self) -> dict:
        return {"id": self.claim_id, "text": self.text, "source": self.source, "level": self.level, "color": self.color,
                "reason": self.reason, "rule_id": self.rule_id}


class GroundingVerifier:
    def __init__(self, nli, *, forbid_check: Callable[[Claim], str | None] | None = None, aliases: Mapping[str, str] | None = None):
        """``aliases``: the pack's names for things its sources call differently (lower-case word -> source word, e.g. soundbox -> device).
        They are applied to the claim only as the entailment model reads it; the claim text that is sent never changes."""
        self.nli, self.forbid_check = nli, forbid_check
        self.aliases = {k.lower(): v.lower() for k, v in (aliases or {}).items()}
        self._alias_re = re.compile(r"\b(" + "|".join(map(re.escape, self.aliases)) + r")\b", re.I) if self.aliases else None

    def _hypothesis(self, text: str) -> str:
        return self._alias_re.sub(lambda m: self.aliases[m.group().lower()], text) if self._alias_re else text

    def verify(self, claims: list[Claim], book: SourceBook) -> list[ClaimVerdict]:
        checks = [check_claim(c, book) for c in claims]
        pairs: list[tuple[str, str]] = []
        index: list[tuple[int, str, str]] = []                       # (claim index, source id, role)
        for i, (c, ck) in enumerate(zip(claims, checks)):
            if not ck.ok:
                continue
            _, body = book.get(c.source)
            pairs.append((verbalise(c.source, body), self._hypothesis(c.text)))
            index.append((i, c.source, "cited"))
        labels = self._labels(pairs)
        cited = {i: labels[n] for n, (i, _, _) in enumerate(index)}

        # corroboration: only for claims whose cited source entails them, against sources of the OTHER kind
        extra_pairs: list[tuple[str, str]] = []
        extra_index: list[tuple[int, str]] = []
        for i, c in enumerate(claims):
            if i in cited and cited[i][0] == ENTAILMENT:
                for sid, body in self._others(book, c.source):
                    extra_pairs.append((verbalise(sid, body), self._hypothesis(c.text)))
                    extra_index.append((i, sid))
        extra = self._labels(extra_pairs)
        extra_by_claim: dict[int, list[tuple[str, str]]] = {}
        for (i, sid), (lab, _) in zip(extra_index, extra):
            extra_by_claim.setdefault(i, []).append((sid, lab))

        out = []
        for i, (c, ck) in enumerate(zip(claims, checks)):
            if not ck.ok:
                why = f"source_not_found: {c.source}" if not ck.source_exists else f"numbers_not_in_source: {', '.join(ck.unmatched_numbers)}"
                out.append(ClaimVerdict(c.id, c.text, c.source, Level.UNSUPPORTED.value, False, "red", why, None, ck.source_exists, ck.numbers_ok))
                continue
            lab, probs = cited[i]
            ev = [Evidence(source_kind(c.source), entailed=lab == ENTAILMENT, contradicted=lab == CONTRADICTION)]
            helpers: list[str] = []
            for sid, l2 in extra_by_claim.get(i, []):
                k = source_kind(sid)
                ev.append(Evidence(k, entailed=l2 == ENTAILMENT))        # other sources can only corroborate; see the module docstring
                if l2 == ENTAILMENT:
                    helpers.append(sid)
            forbid = self.forbid_check(c) if self.forbid_check else None
            if forbid:
                ev.append(Evidence("policy", forbids_rule=forbid))
            lv = level(ev)
            reason = None
            if not lv.sendable:
                reason = (f"forbidden by rule {lv.rule_id}" if lv.rule_id else
                          "a source contradicts the claim" if lv.level is Level.CONTRADICTED else f"the cited source does not entail the claim ({lab})")
            out.append(ClaimVerdict(c.id, c.text, c.source, lv.level.value, lv.sendable, lv.color, reason, lv.rule_id, True, True, lab,
                                    {k: round(v, 4) for k, v in probs.items()}, tuple(helpers)))
        return out

    def _labels(self, pairs: list[tuple[str, str]]) -> list[tuple[str, dict]]:
        """(premise text, claim) -> (label, probs). A source is tried whole AND sentence by sentence: the model answers NEUTRAL for a claim that is an
        exact sentence of a longer document (measured in #51), so a claim is entailed if the source or any ONE of its sentences that is ABOUT the
        claim (``about``) entails it; an unrelated lone sentence is never asked, because a tiny premise can "entail" anything.
        Only the whole source can make it CONTRADICTION: a lone unrelated sentence must not be able to block a claim."""
        flat: list[tuple[str, str]] = []
        spans: list[tuple[int, int]] = []
        for premise, claim in pairs:
            whole, *sentences = _premises(premise)
            ps = [whole] + [x for x in sentences if about(x, claim, self.aliases)]
            spans.append((len(flat), len(ps)))
            flat += [(p, claim) for p in ps]
        got = self.nli.label_batch(flat) if flat else []
        out = []
        for start, n in spans:
            group = got[start:start + n]
            out.append(next((g for g in group if g[0] == ENTAILMENT), group[0]))
        return out

    @staticmethod
    def _others(book: SourceBook, cited: str):
        kind = source_kind(cited)
        stores = {"tool": book.tool_results, "kb": book.kb_chunks, "policy": book.policy_rules}
        for k, store in stores.items():
            if k != kind:
                yield from store.items()
