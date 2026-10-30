"""Source check and numbers check (Bible section 29, steps 1 and 2).

  1. Source check  - the claim's `source` id must exist in THIS turn's SourceBook.
  2. Numbers check - every number in the claim must equal a number in THAT referenced source (after normalisation),
                     never a number from another source or from elsewhere in the turn.

Number sets of a source:
  * tool result (dict): every scalar value, recursively. Numbers are compared by magnitude, so "Rs 8,200" == 8200.00.
    A key ending in `_paise` also contributes value/100 (money is stored in paise, merchants see rupees).
    Identifier-like strings (no spaces, letters and digits mixed: "ST-0042", "call_17") are opaque and contribute NO numbers,
    otherwise an invented "Rs 42" could be justified by an id. ISO dates / datetimes are not treated as identifiers.
  * KB chunk (text) and policy rule (text or dict): same rendering.
Entailment (step 3) and corroboration are other modules' business.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from core.gate.numbers import Num, extract_numbers, number_key

__all__ = ["Claim", "SourceBook", "ClaimCheck", "check_claim", "check_claims", "source_numbers", "source_identifiers"]

_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_OPAQUE = re.compile(r"^[^\s]+$")
# An identifier in claim text: letters first, then digits (T-0042, M-0906, UPI900000000010). Times and units start with a digit (3pm, 30d).
_CLAIM_ID = re.compile(r"(?<![\w-])[A-Za-z]+[-_]?\d[\w-]*")
_KEY_NUMBER = re.compile(r"(?<![a-z0-9])(\d+)(?=[a-z]|_|$)", re.I)


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    source: str


@dataclass
class SourceBook:
    """The sources available THIS turn. Ids: `tool:<name>#<call>`, `kb:<doc>#<section>@<version>`, `policy:<RULE-ID>`."""
    tool_results: dict[str, dict] = field(default_factory=dict)
    kb_chunks: dict[str, str] = field(default_factory=dict)
    policy_rules: dict[str, Any] = field(default_factory=dict)  # rule id -> text or dict

    def add_tool_result(self, source_id: str, body: dict) -> None:
        self.tool_results[source_id] = body

    def add_kb(self, source_id: str, text: str) -> None:
        self.kb_chunks[source_id] = text

    def add_policy(self, source_id: str, body: Any) -> None:
        self.policy_rules[source_id] = body

    def get(self, source_id: str) -> tuple[bool, Any]:
        """(exists, body). The id prefix must match the kind of store it is in."""
        if isinstance(source_id, str):
            if source_id.startswith("tool:") and source_id in self.tool_results:
                return True, self.tool_results[source_id]
            if source_id.startswith("kb:") and source_id in self.kb_chunks:
                return True, self.kb_chunks[source_id]
            if source_id.startswith("policy:") and source_id in self.policy_rules:
                return True, self.policy_rules[source_id]
        return False, None

    def exists(self, source_id: str) -> bool:
        return self.get(source_id)[0]


@dataclass(frozen=True)
class ClaimCheck:
    claim_id: str
    source_exists: bool
    numbers_ok: bool
    unmatched_numbers: list[str]
    reason: str

    @property
    def ok(self) -> bool:
        return self.source_exists and self.numbers_ok


def _scalar_keys(v: Any, key: str, out: set) -> None:
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, Mapping):
        for k, x in v.items():
            for d in _KEY_NUMBER.findall(str(k)):          # a window that lives in the field name: replacements_last_30d -> 30
                out.add(("num", Decimal(d).normalize() if int(d) else Decimal(0)))
            _scalar_keys(x, str(k), out)
    elif isinstance(v, (list, tuple, set, frozenset)):
        for x in v:
            _scalar_keys(x, key, out)
    elif isinstance(v, (int, float, Decimal)):
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            return
        if not d.is_finite():
            return
        if key.lower().endswith("_paise"):                   # money is shown in rupees: only the rupee value is available, never the raw paise
            q = d / 100                                      # integer (offering 512000 for Rs 5,120 let "Rs 512000", 100x, pass: found live, C1)
            out.add(("num", q.normalize() if q != 0 else Decimal(0)))
        else:
            out.add(("num", d.normalize() if d != 0 else Decimal(0)))
    elif isinstance(v, str):
        s = v.strip()
        if _OPAQUE.match(s) and re.search(r"[A-Za-z]", s) and re.search(r"\d", s) and not _ISO_DT.match(s):
            return  # identifier-like string: opaque
        paise = key.lower().endswith("_paise")
        for n in extract_numbers(s):
            u = n.units
            if paise and u is not None:                      # same rule for a paise value stored as text
                q = u / 100
                out.add(("num", q.normalize() if q != 0 else Decimal(0)))
            else:
                out.add(number_key(n))
    else:
        _scalar_keys(str(v), key, out)


def source_identifiers(body: Any) -> set[str]:
    """Every identifier-like string in a source (the strings ``_scalar_keys`` treats as opaque), upper-cased."""
    out: set[str] = set()
    if isinstance(body, Mapping):
        for x in body.values():
            out |= source_identifiers(x)
    elif isinstance(body, (list, tuple, set, frozenset)):
        for x in body:
            out |= source_identifiers(x)
    elif isinstance(body, str):
        out |= {m.upper() for m in _CLAIM_ID.findall(body)}
    return out


def source_numbers(body: Any) -> set[tuple[str, object]]:
    """The set of comparison keys (see numbers.number_key) of every number in a source body."""
    out: set = set()
    _scalar_keys(body, "", out)
    return out


def check_claim(claim: Claim, book: SourceBook) -> ClaimCheck:
    cid = getattr(claim, "id", "")
    exists, body = book.get(getattr(claim, "source", None))
    text = getattr(claim, "text", "")
    ids = list(_CLAIM_ID.finditer(text))
    for m in reversed(ids):                                  # an identifier's digits are not a quantity: T-0042 is not the number 42
        text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    nums: list[Num] = extract_numbers(text, english_one=True)
    if not exists:
        return ClaimCheck(cid, False, False, [m.group() for m in ids] + [n.raw for n in nums], "source_not_found")
    avail, known = source_numbers(body), source_identifiers(body)
    unmatched = [m.group() for m in ids if m.group().upper() not in known] + [n.raw for n in nums if number_key(n) not in avail]
    if unmatched:
        return ClaimCheck(cid, True, False, unmatched, "numbers_not_in_source")
    return ClaimCheck(cid, True, True, [], "ok")


def check_claims(claims: Iterable[Claim], book: SourceBook) -> list[ClaimCheck]:
    return [check_claim(c, book) for c in claims]
