"""Rep and technician briefings (#110).

A briefing is assembled from VERIFIED CLAIMS only: each bullet is a ``Claim`` whose source is a system read in this turn's ``SourceBook`` (the lead row,
the visit history, the stock on the rep). A bullet whose fact is absent is dropped, never filled in. The set of claims goes through the grounding gate
(verify -> render in the person's language -> faithfulness -> compliance). Nothing is delivered unless the gate says SEND, and delivery is an
ActionIntent for the executor (policy, ledger, idempotency key): this module never sends. No fixed phrase is invented here: conversational lines come
from packs/paytm_merchant/phrases.yaml through the renderer.

Depth = experience x lead value (decided in code):
  new rep (< NEW_MONTHS) on a high-value lead -> DETAILED (every bullet);  veteran (>= SENIOR_MONTHS) on a low-value lead -> SHORT (3 bullets);
  everyone else -> STANDARD (5 bullets).
Follow-up questions are answered only from the same sources (``answer_followup``); a question with no source returns None so a person answers it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.actions.types import ActionIntent, Principal
from core.gate.pipeline import SEND, GateOutcome
from core.gate.sources import Claim, SourceBook
from field.profiles import RepProfile

NEW_MONTHS, SENIOR_MONTHS = 12, 24
HIGH_VALUE_PAISE = 5_000_000
DETAILED, STANDARD, SHORT = "DETAILED", "STANDARD", "SHORT"
PRINCIPAL = Principal("AGENT", "munim")
LEAD_SOURCE = "tool:get_lead#{lead_id}"
VISIT_SOURCE = "tool:get_visit_history#{lead_id}"
STOCK_SOURCE = "tool:get_rep_stock#{rep_id}"
FIELD_ORDER = ("who", "value", "last_visit", "objection", "product_stock", "address", "best_time", "documents")        # most useful first
DEPTH_COUNT = {SHORT: 3, STANDARD: 5, DETAILED: len(FIELD_ORDER)}
FOLLOWUP_TOPICS = {"value": "value", "price": "value", "address": "address", "where": "address", "time": "best_time", "when": "best_time",
                   "objection": "objection", "document": "documents", "stock": "product_stock", "last": "last_visit", "who": "who"}


def depth_for(experience_months: int, value_paise: int) -> str:
    if experience_months < NEW_MONTHS and value_paise >= HIGH_VALUE_PAISE:
        return DETAILED
    if experience_months >= SENIOR_MONTHS and value_paise < HIGH_VALUE_PAISE:
        return SHORT
    return STANDARD


def brief_language(rep: RepProfile) -> str:
    """The language the rep is native in, regional before English."""
    native = sorted(l for l, lvl in rep.languages.items() if lvl == "native")
    return next((l for l in native if l != "en-IN"), native[0] if native else "en-IN")


def _rupees(paise: int) -> str:
    return f"{paise // 100}" if paise % 100 == 0 else f"{paise / 100:.2f}"


def _bullets(lead: dict, visits: dict, stock: dict, lead_src: str, visit_src: str, stock_src: str) -> dict[str, Claim | None]:
    c = lambda key, text, src: Claim(f"b_{key}", text, src)       # noqa: E731
    return {
        "who": c("who", f"The lead is {lead['name']}, a {lead['category']} shop.", lead_src) if lead.get("name") and lead.get("category") else None,
        "value": c("value", f"The lead value is {_rupees(lead['value_paise'])} rupees.", lead_src) if lead.get("value_paise") is not None else None,
        "last_visit": c("last_visit", f"The last visit outcome was {visits['last_outcome']}.", visit_src) if visits.get("last_outcome") else None,
        "objection": c("objection", f"The merchant's main objection was {visits['objection']}.", visit_src) if visits.get("objection") else None,
        "product_stock": c("product_stock", f"You have {stock['count']} {stock['product']} units in stock.", stock_src) if stock.get("product") and stock.get("count") is not None else None,
        "address": c("address", f"The address is {lead['address']}.", lead_src) if lead.get("address") else None,
        "best_time": c("best_time", f"The best time to visit is {lead['best_time']}.", lead_src) if lead.get("best_time") else None,
        "documents": c("documents", f"Ask the merchant for {lead['documents']}.", lead_src) if lead.get("documents") else None,
    }


@dataclass
class Brief:
    lead_id: str
    rep_id: str
    depth: str
    language: str
    claims: list[Claim]
    book: SourceBook
    fields: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)      # fields wanted at this depth but not available from a system read


def build_brief(rep: RepProfile, lead: dict, visits: dict, stock: dict) -> Brief:
    """``lead`` / ``visits`` / ``stock`` are system reads (dicts). Returns the claims and the book they cite; nothing is invented."""
    lead_src, visit_src = LEAD_SOURCE.format(lead_id=lead["lead_id"]), VISIT_SOURCE.format(lead_id=lead["lead_id"])
    stock_src = STOCK_SOURCE.format(rep_id=rep.rep_id)
    book = SourceBook()
    book.add_tool_result(lead_src, lead)
    book.add_tool_result(visit_src, visits)
    book.add_tool_result(stock_src, stock)
    depth = depth_for(rep.experience_months, lead.get("value_paise", 0))
    available = _bullets(lead, visits, stock, lead_src, visit_src, stock_src)
    wanted = list(FIELD_ORDER)
    chosen = [k for k in wanted if available[k] is not None][:DEPTH_COUNT[depth]]
    dropped = [k for k in wanted[:DEPTH_COUNT[depth]] if available[k] is None]
    return Brief(lead["lead_id"], rep.rep_id, depth, brief_language(rep), [available[k] for k in chosen], book, chosen, dropped)


def answer_followup(brief: Brief, question: str) -> list[Claim] | None:
    """The claims that answer a follow-up, from the same sources, or None when no source covers it (a person answers)."""
    q = question.lower()
    for word, topic in FOLLOWUP_TOPICS.items():
        if word in q:
            full = {c.id: c for c in _all_claims(brief)}
            hit = full.get(f"b_{topic}")
            return [hit] if hit else None
    return None


def _all_claims(brief: Brief) -> list[Claim]:
    lead_src = LEAD_SOURCE.format(lead_id=brief.lead_id)
    ok, lead = brief.book.get(lead_src)
    _, visits = brief.book.get(VISIT_SOURCE.format(lead_id=brief.lead_id))
    _, stock = brief.book.get(STOCK_SOURCE.format(rep_id=brief.rep_id))
    return [c for c in _bullets(lead, visits, stock, lead_src, VISIT_SOURCE.format(lead_id=brief.lead_id), STOCK_SOURCE.format(rep_id=brief.rep_id)).values() if c]


def run_gate(gate, brief: Brief, claims: list[Claim] | None = None, *, case_id: str | None = None) -> GateOutcome:
    """Every briefing and every follow-up answer goes through the grounding gate; there is no other way to produce text for the rep here."""
    return gate.run(claims=claims if claims is not None else brief.claims, book=brief.book, language=brief.language, case_id=case_id)


def deliver(outcome: GateOutcome, brief: Brief, submit) -> object | None:
    """Hand the gated text to the executor path. ``submit`` is ``Executor.submit``. Nothing goes if the gate did not say SEND."""
    if outcome.status != SEND or not outcome.text:
        return None
    intent = ActionIntent("notify_field_person", {"person_id": brief.rep_id, "body": outcome.text, "language": brief.language, "subject": f"brief:{brief.lead_id}"},
                          step_seq=0, principal=PRINCIPAL, playbook_id="FIELD_BRIEF", step="deliver_brief", proactive=True, channel="app",
                          allowlist=("notify_field_person",), facts={"lead_id": brief.lead_id})
    return submit(intent)
