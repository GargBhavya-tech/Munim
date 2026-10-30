"""Handoff packet (#54) and handoff queue with return-to-agent (#55), Bible section 32.

* ``HandoffService.create_packet`` builds the packet JSON, verifies the summary against the ledger, stores it in ``handoffs``.
* Summary sentences cite ledger sequence numbers as ``ev:<seq>``. A deterministic checker drops any sentence whose citations do not
  exist (for this case) or do not contain the action / status the sentence talks about. The summarizer is injected (it may be an LLM);
  the default ledger summarizer only restates ledger entries and cannot invent anything.
* The queue is ordered by deadline, then lane, then age. A human replies, acts, undoes, closes, or returns the case to the agent.
* ``return_to_agent`` appends a HUMAN_RETURNED event whose PAYLOAD carries the instruction (and the agent's last message, so it is not
  repeated). The engine's wake-up logic then resumes at diagnose with that event as the turn's event, so the note is in the context.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.actions.types import ActionIntent, Principal
from core.audit.ledger import Ledger, LedgerEntry
from core.engine.cases import CaseRepo
from core.engine.clock import SimClock, iso, parse_iso
from core.engine.events import append_event
from core.engine.roles import ROLES
from core.memory.store import next_id, write_txn

SIM = "SIM "
LANE_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STATUSES = ("OPEN", "RETURNED", "CLOSED")
VERDICTS = ("needed", "not_needed")
SUPERVISOR_ROLE = "ops_admin"      # may work every queue; every other role only its own

LANGUAGE_NAMES = {"kn": "Kannada", "hi": "Hindi", "en": "English", "hinglish": "Hinglish", "ta": "Tamil", "te": "Telugu",
                  "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati"}


class HandoffError(Exception):
    pass


class PacketInvalid(HandoffError):
    pass


class HandoffPermissionError(HandoffError):
    pass


class HandoffStateError(HandoffError):
    pass


# ------------------------------------------------------------------------------------------ rules by reason class
@dataclass(frozen=True)
class ReasonClass:
    name: str
    owner_role: str
    hours: int
    step: str                       # "{who} to ..." sentence start; "{language}" and "{hours}" are filled in
    codes: frozenset                # reason codes (before any ': detail') and policy rule ids that belong here


_CLASSES: tuple[ReasonClass, ...] = (
    ReasonClass("RISK", "risk_reviewer", 2,
                "Risk reviewer to decide on the hold or flag; call the merchant in {language} within {hours} hours with the outcome.",
                frozenset({"SETTLEMENT_HELD_RISK", "FRAUD_SIGNAL", "RISK-HOLD-001"})),
    ReasonClass("REGULATORY", "ops_admin", 2,
                "Ops lead to review the legal or regulatory mention before anyone replies; contact the merchant in {language} within {hours} hours. Make no commitments before the review.",
                frozenset({"REGULATORY_MENTION"})),
    ReasonClass("HUMAN_REQUEST", "support_executive", 2,
                "Support executive to call the merchant in {language} within {hours} hours; the merchant asked for a person.",
                frozenset({"WANTS_HUMAN"})),
    ReasonClass("KYC_MISMATCH", "risk_reviewer", 4,
                "Risk reviewer to verify the bank account name against the KYC name; call the merchant in {language} within {hours} hours.",
                frozenset({"NAME_MISMATCH"})),
    ReasonClass("MONEY", "ops_admin", 4,
                "Ops lead to approve or decline the action above the money limit; call the merchant in {language} within {hours} hours.",
                frozenset({"AMOUNT_ABOVE_LIMIT", "MONEY-LIMIT-001"})),
    ReasonClass("PRICE_TERMS", "sales_lead", 4,
                "Sales lead to decide the price, term or waiver outside the approved band; call the merchant in {language} within {hours} hours.",
                frozenset({"OUT_OF_BAND", "WAIVER-BAND-001"})),
    ReasonClass("TOOL", "ops_admin", 2,
                "Ops lead to check the failing system; tell the merchant in {language} within {hours} hours that we are on it.",
                frozenset({"TOOL_FAILURE"})),
    ReasonClass("AGENT_FAULT", "support_executive", 4,
                "Support executive to take over the conversation in {language} within {hours} hours; the assistant could not continue safely.",
                frozenset({"GROUNDING_FAILED_TWICE", "FAITHFULNESS_FAILED_TWICE", "INVALID_OUTPUT_TWICE", "LOOP", "SOFT_SCORE",
                           "BLUE_REPEATED", "UNVERIFIABLE_CLAIM", "OUTSIDE_KNOWLEDGE"})),
)
DEFAULT_CLASS = ReasonClass("GENERAL", "support_executive", 4,
                            "Support executive to review the case and contact the merchant in {language} within {hours} hours.", frozenset())
for _c in _CLASSES + (DEFAULT_CLASS,):
    assert _c.owner_role in ROLES


def reason_code(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def classify(reasons: Iterable[str], rule_ids: Iterable[str] = ()) -> ReasonClass:
    """The most urgent class among the reasons and rule ids (smallest deadline; ties by table order). Not a model."""
    keys = {reason_code(r) for r in reasons} | set(rule_ids)
    matched = [c for c in _CLASSES if c.codes & keys]
    if not matched:
        return DEFAULT_CLASS
    return min(matched, key=lambda c: (c.hours, _CLASSES.index(c)))


def language_name(code: str | None) -> str:
    if not code:
        return "the merchant's language"
    return LANGUAGE_NAMES.get(code.split("-")[0].lower(), code)


# ------------------------------------------------------------------------------------------ summary + checker
_ACTION_RE = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
_REF_RE = re.compile(r"\[ev:\d+(?:,\s*ev:\d+)*\]")


def _walk(obj: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def _entry_has(entry: LedgerEntry, key: str, value: Any) -> bool:
    keys = ("status", "state", "type", "rule_ids", "reasons") if key == "status" else (key,)
    for k, v in _walk(entry.payload):
        if k in keys and (v == value or (isinstance(v, list) and value in v)):
            return True
    return False


def _entry_text(entry: LedgerEntry) -> str:
    return json.dumps(entry.payload, sort_keys=True, ensure_ascii=False)


def sentence_ok(sentence: Mapping, entries: Mapping[int, LedgerEntry], case_id: str) -> tuple[bool, str]:
    """Deterministic check of one summary sentence. Returns (ok, why-not)."""
    text, refs = sentence.get("text"), sentence.get("refs")
    if not isinstance(text, str) or not text.strip():
        return False, "empty sentence"
    if not refs or not all(isinstance(r, int) and not isinstance(r, bool) for r in refs):
        return False, "no event references"
    cited = []
    for r in refs:
        e = entries.get(r)
        if e is None or e.case_id != case_id:
            return False, f"ev:{r} does not exist for this case"
        cited.append(e)
    for key, value in (sentence.get("expects") or {}).items():
        if not any(_entry_has(e, key, value) for e in cited):
            return False, f"cited events do not contain {key}={value}"
    blob = " ".join(_entry_text(e) for e in cited)
    for name in _ACTION_RE.findall(text):          # an action the sentence names must be in a cited event
        if name not in blob:
            return False, f"cited events do not mention action {name}"
    for tok in _TOKEN_RE.findall(text):            # a status / state / event type the sentence names must be in a cited event
        if tok not in blob:
            return False, f"cited events do not mention {tok}"
    return True, ""


def verify_summary(sentences: Sequence[Mapping], entries: Iterable[LedgerEntry], case_id: str) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). A dropped sentence carries a ``why`` field. Nothing unverifiable survives."""
    by_seq = {e.seq: e for e in entries}
    kept, dropped = [], []
    for s in sentences:
        ok, why = sentence_ok(s, by_seq, case_id)
        s2 = {"text": s.get("text"), "refs": list(s.get("refs") or []), **({"expects": dict(s["expects"])} if s.get("expects") else {})}
        if ok:
            kept.append(s2)
        else:
            dropped.append({**s2, "why": why})
    return kept, dropped


def ledger_summarizer(case: Any, entries: Sequence[LedgerEntry], context: Mapping | None = None) -> list[dict]:
    """Default summarizer: restates the case ledger, one sentence per notable entry, each citing that entry. Never invents."""
    out: list[tuple[int, dict]] = []
    events, states, results, refusals = [], [], [], []
    for e in entries:
        p = e.payload
        if e.kind == "EVENT" and p.get("type"):
            events.append((e.seq, {"text": f"Event {p['type']} was received.", "refs": [e.seq], "expects": {"status": p["type"]}}))
        elif e.kind == "STATE" and p.get("state"):
            states.append((e.seq, {"text": f"The case moved to state {p['state']}.", "refs": [e.seq], "expects": {"status": p["state"]}}))
        elif e.kind == "ACTION_RESULT" and p.get("cls") != "R0" and p.get("action"):
            results.append((e.seq, {"text": f"Action {p['action']} finished with status {p.get('status')}.", "refs": [e.seq],
                                    "expects": {"action": p["action"], "status": p.get("status")}}))
        elif e.kind == "POLICY" and p.get("allow") is False and p.get("action"):
            refusals.append((e.seq, {"text": f"Action {p['action']} was not allowed by policy.", "refs": [e.seq], "expects": {"action": p["action"]}}))
    for group, n in ((events, 3), (states, 3), (results, 5), (refusals, 2)):
        out.extend(group[-n:])
    return [s for _, s in sorted(out, key=lambda t: t[0])]


Summarizer = Callable[[Any, Sequence[LedgerEntry], Mapping], Sequence[Mapping]]


# ------------------------------------------------------------------------------------------ packet schema
PACKET_FIELDS: dict[str, type | tuple] = {
    "case_id": str, "playbook": str, "lane": str, "escalated_at": str, "reasons": list, "policy_rule_ids": list, "summary": str,
    "summary_sentences": list, "facts": dict, "sentiment_risk": dict, "actions_taken": list, "open_compensations": list,
    "claims_blocked": list, "other_open_cases": list, "recommended_next_step": str, "deadline": str, "owner_role": str,
}
FACT_FIELDS = ("language", "segment", "amount_inr", "done_fields")


def validate_packet(p: Mapping) -> list[str]:
    """Schema errors (empty list = valid). Every field of the Bible packet must be present and typed."""
    errs: list[str] = []
    for k, t in PACKET_FIELDS.items():
        if k not in p:
            errs.append(f"missing field {k}")
        elif not isinstance(p[k], t):
            errs.append(f"field {k} must be {t.__name__}")
    if errs:
        return errs
    for k in ("escalated_at", "deadline"):
        if not p[k].startswith(SIM):
            errs.append(f"{k} must start with 'SIM '")
        else:
            try:
                parse_iso(p[k][len(SIM):])
            except ValueError:
                errs.append(f"{k} is not an ISO time")
    if p["lane"] not in LANE_ORDER:
        errs.append("lane must be P0..P3")
    if not p["reasons"] or not all(isinstance(r, str) for r in p["reasons"]):
        errs.append("reasons must be a non-empty list of strings")
    if not all(isinstance(r, str) for r in p["policy_rule_ids"]):
        errs.append("policy_rule_ids must be strings")
    if p["owner_role"] not in ROLES:
        errs.append("owner_role is not a known role")
    for f in FACT_FIELDS:
        if f not in p["facts"]:
            errs.append(f"facts missing {f}")
    for f in ("frustration", "fraud_flags", "regulatory_mentions"):
        if f not in p["sentiment_risk"]:
            errs.append(f"sentiment_risk missing {f}")
    if not p["summary_sentences"]:
        errs.append("summary has no verified sentence")
    for s in p["summary_sentences"]:
        if not (isinstance(s, dict) and s.get("text") and s.get("refs") and all(isinstance(r, int) for r in s["refs"])):
            errs.append("every summary sentence needs text and event refs")
            break
    for a in p["actions_taken"]:
        if not (isinstance(a, dict) and "action" in a and "id" in a and "compensable_until" in a):
            errs.append("actions_taken entries need action, id, compensable_until")
            break
    return errs


# ------------------------------------------------------------------------------------------ the service
@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    case_id: str
    status: str
    owner_role: str | None
    deadline: str
    created_at: str
    verdict: str | None
    packet: dict

    @property
    def lane(self) -> str:
        return self.packet.get("lane", "P3")


def _norm_msg(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").casefold()).strip()


def repeats_last_message(candidate: str | None, last_message: str | None) -> bool:
    """True when ``candidate`` says the same thing as the agent's previous message (case, spacing and punctuation ignored)."""
    return bool(candidate) and bool(last_message) and _norm_msg(candidate) == _norm_msg(last_message)


class HandoffService:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger: Ledger, *, summarizer: Summarizer | None = None,
                 executor: Any = None, on_return: Callable[[dict], Any] | None = None):
        """``executor`` runs human replies/acts/undos through the same pipeline as the agent. ``on_return(event)`` is called after the
        return is committed, typically ``engine.handle_event``; without it the caller feeds the returned event to the engine."""
        self.conn, self.clock, self.ledger = conn, clock, ledger
        self.summarizer = summarizer or ledger_summarizer
        self.executor, self.on_return = executor, on_return
        self.repo = CaseRepo(conn, clock, ledger)

    # -------------------------------------------------------------------------------------- packet
    def create_packet(self, case: Any, reasons: Sequence[str], violations: Sequence[Any] = (), *, facts: Mapping | None = None,
                      sentiment_risk: Mapping | None = None, claims_blocked: Sequence[Mapping] = (),
                      summarizer: Summarizer | None = None) -> Handoff:
        """Build, verify, validate and store the packet. ``facts`` may carry language/segment/amount_paise|amount_inr/done_fields."""
        facts = dict(facts or {})
        reasons = [str(r) for r in reasons]
        rule_ids = sorted({v.rule_id for v in violations})
        entries = self.ledger.entries(case.case_id)
        proposed = (summarizer or self.summarizer)(case, entries, {"reasons": reasons, "rule_ids": rule_ids, "facts": facts})
        kept, dropped = verify_summary(proposed, entries, case.case_id)
        if not kept:   # the injected summarizer produced nothing verifiable: fall back to the ledger so the packet is never empty
            kept, extra = verify_summary(ledger_summarizer(case, entries), entries, case.case_id)
            dropped += extra
        cls = classify(reasons, rule_ids)
        for v in violations:   # a policy route names the reviewer more precisely than the class table
            if getattr(v, "route", None) in ROLES and cls.name in ("PRICE_TERMS", "MONEY", "GENERAL"):
                cls = ReasonClass(cls.name, v.route, cls.hours, cls.step, cls.codes)
                break
        now = self.clock.now()
        language = facts.get("language") or case.language
        merchant = self.conn.execute("SELECT segment FROM merchants WHERE merchant_id = ?", (case.merchant_id,)).fetchone() if case.merchant_id else None
        amount_inr = facts.get("amount_inr")
        if amount_inr is None and facts.get("amount_paise") is not None:
            amount_inr = int(facts["amount_paise"]) // 100
        sr = {"frustration": 0.0, "fraud_flags": [], "regulatory_mentions": [], **dict(sentiment_risk or {})}
        packet = {
            "case_id": case.case_id, "playbook": case.playbook_id, "lane": case.lane,
            "escalated_at": SIM + iso(now), "reasons": reasons, "policy_rule_ids": rule_ids,
            "summary": " ".join(f"{s['text']} [{', '.join(f'ev:{r}' for r in s['refs'])}]" for s in kept),
            "summary_sentences": kept,
            "facts": {"language": language, "segment": facts.get("segment") or (merchant["segment"] if merchant else None),
                      "amount_inr": amount_inr, "done_fields": dict(facts.get("done_fields") or {})},
            "sentiment_risk": sr,
            "actions_taken": self._actions_taken(case.case_id),
            "open_compensations": self._open_compensations(case.case_id),
            "claims_blocked": [dict(c) for c in claims_blocked],
            "other_open_cases": self._other_open_cases(case),
            "recommended_next_step": cls.step.format(language=language_name(language), hours=cls.hours),
            "deadline": SIM + iso(now + timedelta(hours=cls.hours)),
            "owner_role": cls.owner_role,
        }
        errs = validate_packet(packet)
        if errs:
            raise PacketInvalid("; ".join(errs))
        with write_txn(self.conn):
            hid = next_id(self.conn, "HO")
            self.conn.execute(
                "INSERT INTO handoffs(handoff_id, case_id, packet_json, status, owner_role, deadline, created_at) VALUES (?,?,?,?,?,?,?)",
                (hid, case.case_id, json.dumps(packet, sort_keys=True, ensure_ascii=False), "OPEN", cls.owner_role,
                 packet["deadline"][len(SIM):], iso(now)))
            self.ledger.append(kind="ESCALATION", actor="SYSTEM", case_id=case.case_id,
                               payload={"handoff_id": hid, "owner_role": cls.owner_role, "deadline": packet["deadline"], "reasons": reasons,
                                        "dropped_sentences": len(dropped)})
        return self.get(hid)

    def _actions_taken(self, case_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT a.action_id, a.name, a.cls, c.compensable_until FROM actions a LEFT JOIN compensations c ON c.action_id = a.action_id "
            "WHERE a.case_id = ? AND a.status = 'OK' AND a.cls != 'R0' ORDER BY a.rowid", (case_id,)).fetchall()
        out = []
        for r in rows:
            until = r["compensable_until"]
            out.append({"action": r["name"], "id": r["action_id"],
                        "compensable_until": (SIM + until) if until and not until.startswith("9999") else None})
        return out

    def _open_compensations(self, case_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT c.compensation_json, c.compensable_until FROM compensations c JOIN actions a ON a.action_id = c.action_id "
            "WHERE a.case_id = ? AND c.status = 'OPEN' ORDER BY a.rowid", (case_id,)).fetchall()
        now = self.clock.now()
        out = []
        for r in rows:
            if now > parse_iso(r["compensable_until"]):
                continue       # the window has closed: Undo can no longer run, so it is not offered
            comp = json.loads(r["compensation_json"])
            out.append(f"{comp['action']}({', '.join(str(v) for v in comp['args'].values())})")
        return out

    def _other_open_cases(self, case: Any) -> list[str]:
        subject_col, subject = ("merchant_id", case.merchant_id) if case.merchant_id else ("lead_id", case.lead_id)
        rows = self.conn.execute(f"SELECT playbook_id, state FROM cases WHERE {subject_col} = ? AND case_id != ? "
                                 "AND state NOT IN ('RESOLVED','DECLINED') ORDER BY opened_at, case_id", (subject, case.case_id)).fetchall()
        return [f"{r['playbook_id']} ({r['state'].lower()})" for r in rows]

    # -------------------------------------------------------------------------------------- reads
    def _row(self, r: sqlite3.Row) -> Handoff:
        return Handoff(r["handoff_id"], r["case_id"], r["status"], r["owner_role"], r["deadline"], r["created_at"], r["verdict"],
                       json.loads(r["packet_json"]))

    def get(self, handoff_id: str) -> Handoff:
        r = self.conn.execute("SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)).fetchone()
        if r is None:
            raise KeyError(handoff_id)
        return self._row(r)

    def for_case(self, case_id: str) -> list[Handoff]:
        rows = self.conn.execute("SELECT * FROM handoffs WHERE case_id = ? ORDER BY created_at, handoff_id", (case_id,)).fetchall()
        return [self._row(r) for r in rows]

    def list(self, role: str | None = None, status: str | None = "OPEN") -> list[Handoff]:
        """The queue: earliest deadline first, ties by lane (P0 first) then age. ``role`` filters to that role's queue
        (``ops_admin`` sees every queue); ``status=None`` lists all statuses."""
        sql, args = "SELECT * FROM handoffs WHERE 1=1", []
        if status is not None:
            sql, args = sql + " AND status = ?", args + [status]
        if role is not None and role != SUPERVISOR_ROLE:
            sql, args = sql + " AND owner_role = ?", args + [role]
        items = [self._row(r) for r in self.conn.execute(sql, args).fetchall()]
        items.sort(key=lambda h: (parse_iso(h.deadline), LANE_ORDER.get(h.lane, 9), parse_iso(h.created_at), h.handoff_id))
        return items

    # -------------------------------------------------------------------------------------- human actions
    def _authorise(self, h: Handoff, principal: Principal, *, need_open: bool = True) -> None:
        if principal.kind != "HUMAN" or principal.role not in ROLES:
            raise HandoffPermissionError("only a known human role may work a handoff")
        if principal.role == "auditor":
            raise HandoffPermissionError("auditor is read-only")
        if principal.role != SUPERVISOR_ROLE and principal.role != h.owner_role:
            raise HandoffPermissionError(f"{h.handoff_id} is in the {h.owner_role} queue, not {principal.role}")
        if need_open and h.status != "OPEN":
            raise HandoffStateError(f"{h.handoff_id} is {h.status}, not OPEN")

    def _human_entry(self, h: Handoff, principal: Principal, op: str, **payload) -> LedgerEntry:
        return self.ledger.append(kind="HUMAN_ACTION", actor=principal.ledger_actor, role=principal.role, case_id=h.case_id,
                                  payload={"handoff_id": h.handoff_id, "op": op, **payload})

    def _human_seq(self, case_id: str) -> int:
        n = self.conn.execute("SELECT COUNT(*) FROM ledger WHERE case_id = ? AND kind = 'HUMAN_ACTION'", (case_id,)).fetchone()[0]
        return 1000 + n          # step_seq range kept apart from the engine's own sequence

    def _require_executor(self) -> Any:
        if self.executor is None:
            raise HandoffError("no executor configured: a human action must go through the action pipeline")
        return self.executor

    def reply(self, handoff_id: str, principal: Principal, body: str, *, channel: str = "whatsapp", language: str | None = None):
        """The human answers the merchant. Goes through the executor as send_message (policy, consent, ledger), as that human."""
        h = self.get(handoff_id)
        self._authorise(h, principal)
        ex = self._require_executor()
        case_row = self.conn.execute("SELECT merchant_id, playbook_id, lane, language FROM cases WHERE case_id = ?", (h.case_id,)).fetchone()
        intent = ActionIntent("send_message", {"merchant_id": case_row["merchant_id"], "channel": channel, "body": body,
                                               "language": language or case_row["language"] or "en", "case_id": h.case_id},
                              case_id=h.case_id, step_seq=self._human_seq(h.case_id), principal=principal,
                              playbook_id=case_row["playbook_id"], lane=case_row["lane"], channel=channel)
        out = ex.submit(intent)
        self._human_entry(h, principal, "reply", status=out.status)
        return out

    def act(self, handoff_id: str, principal: Principal, action: str, args: Mapping):
        """The human runs a registered action: same executor, same policy, same ledger, with the human as principal."""
        h = self.get(handoff_id)
        self._authorise(h, principal)
        ex = self._require_executor()
        case_row = self.conn.execute("SELECT playbook_id, lane FROM cases WHERE case_id = ?", (h.case_id,)).fetchone()
        out = ex.submit(ActionIntent(action, dict(args), case_id=h.case_id, step_seq=self._human_seq(h.case_id), principal=principal,
                                     playbook_id=case_row["playbook_id"], lane=case_row["lane"]))
        self._human_entry(h, principal, "act", action=action, status=out.status)
        return out

    def undo(self, handoff_id: str, principal: Principal, action_id: str):
        h = self.get(handoff_id)
        self._authorise(h, principal)
        out = self._require_executor().undo(action_id, principal)
        self._human_entry(h, principal, "undo", action_id=action_id, status=out.status)
        return out

    def close(self, handoff_id: str, principal: Principal, note: str | None = None) -> Handoff:
        """The human resolves the case themselves: handoff CLOSED, case RESOLVED."""
        h = self.get(handoff_id)
        self._authorise(h, principal)
        with write_txn(self.conn):
            self._human_entry(h, principal, "close", **({"note": note} if note else {}))
            self.conn.execute("UPDATE handoffs SET status = 'CLOSED' WHERE handoff_id = ?", (handoff_id,))
            case = self.repo.load(h.case_id)
            if case.state == "ESCALATED":
                self.repo.transition(case, "RESOLVED", reason="closed_by_human")
        return self.get(handoff_id)

    def last_agent_message(self, case_id: str) -> str | None:
        for e in reversed(self.ledger.entries(case_id)):
            if e.kind == "MESSAGE_SENT" and e.payload.get("text"):
                return e.payload["text"]
        return None

    def return_to_agent(self, handoff_id: str, principal: Principal, instruction: str) -> dict:
        """Give the case back to Munim with an instruction ("continue; replacement approved").

        Records HUMAN_ACTION, marks the handoff RETURNED, appends a HUMAN_RETURNED event whose payload carries the instruction, the
        agent's last message (``do_not_repeat``), and who returned it. Returns the event as a dict the engine accepts
        (``Engine.handle_event``); with ``on_return`` set it is also handed over after the commit. The engine moves ESCALATED ->
        RETURNED_TO_AGENT -> ACTIVE and diagnoses against live state, with this event as the turn's event."""
        instruction = (instruction or "").strip()
        if not instruction:
            raise HandoffError("a return needs an instruction")
        h = self.get(handoff_id)
        self._authorise(h, principal)
        case = self.repo.load(h.case_id)
        if case.state != "ESCALATED":
            raise HandoffStateError(f"{case.case_id} is {case.state}; only an ESCALATED case can be returned")
        event = {
            "event_id": f"HR-{handoff_id}", "type": "HUMAN_RETURNED", "merchant_id": case.merchant_id, "case_id": case.case_id,
            "payload": {"handoff_id": handoff_id, "instruction": instruction, "returned_by": principal.id, "role": principal.role,
                        "do_not_repeat": self.last_agent_message(case.case_id), "reasons": h.packet["reasons"]},
        }
        with write_txn(self.conn):
            self._human_entry(h, principal, "return", instruction=instruction)
            self.conn.execute("UPDATE handoffs SET status = 'RETURNED' WHERE handoff_id = ?", (handoff_id,))
            append_event(self.conn, event_id=event["event_id"], type="HUMAN_RETURNED", payload=event["payload"], ts_sim=self.clock.now_iso(),
                         merchant_id=case.merchant_id, case_id=case.case_id)
        if self.on_return is not None:
            self.on_return(event)
        return event

    # -------------------------------------------------------------------------------------- verdict
    def record_verdict(self, handoff_id: str, verdict: str, principal: Principal | None = None) -> Handoff:
        """The human's label for escalation precision: 'needed' or 'not_needed'. May be set once, may be corrected by the same call."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        h = self.get(handoff_id)
        if principal is not None:
            self._authorise(h, principal, need_open=False)
        with write_txn(self.conn):
            self.conn.execute("UPDATE handoffs SET verdict = ? WHERE handoff_id = ?", (verdict, handoff_id))
            if principal is not None:
                self._human_entry(h, principal, "verdict", verdict=verdict)
        return self.get(handoff_id)

    def escalation_precision(self) -> float | None:
        """needed / (needed + not_needed) over labelled handoffs; None when nothing is labelled yet."""
        r = self.conn.execute("SELECT SUM(verdict = 'needed') AS n, COUNT(verdict) AS t FROM handoffs").fetchone()
        return None if not r["t"] else r["n"] / r["t"]
