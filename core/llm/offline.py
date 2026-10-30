"""Offline mode: a rule-based stand-in for the model, for a demo with no Sarvam key (or no network).

It replaces ONLY the two model calls the turn agent makes, behind the same ``ChatProvider`` interface the real client uses:
  * the DECISION call: it reads the same context the model would (case facts with their source ids, allowed actions, the merchant's words), detects the
    intent with the pack's own phrase lists (core.engine.intents), and returns the same ``TurnDecision`` JSON: claims copied from the system reads in the
    verbaliser's own words, and at most one move from a small per-playbook table, only if the guard's allow-list already contains it;
  * the RENDER call: it writes each verified claim as one sentence, in English, or in Hinglish through a fixed template.
Everything else is the production path and is NOT bypassed: the decision guard, policy (default-deny), the executor and its idempotency keys, the grounding gate
(source check, the real entailment model, compliance lines) and the ledger. Every number a merchant sees is still read from a system. A claim with no source is
still deleted; a reply the gate cannot support is still handed to a person.

Honest limits (also in docs/LIMITATIONS.md): it understands only what the phrase lists understand (18 intents), it cannot write Hindi in Devanagari or Kannada
(those merchants get English claim sentences plus the fixed phrases), and its back-translation for the faithfulness check is an exact lookup of the sentences
it wrote itself, so that one check is trivially satisfied in this mode. The claims-versus-sources entailment check still runs on the real model.
Hinglish templates use English nouns and ``hai``; they are flagged for native review like every other non-English line.
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.engine.intents import detect_intent
from core.gate.verify import _fmt_value, _label
from core.llm.sarvam_http import ChatResult
from core.memory.context import LATEST_CLOSE, LATEST_OPEN

MODEL_ID = "offline-rules-v1"
MAX_CLAIMS = 4
_HINGLISH = {"hai", "hain", "nahi", "nahin", "mera", "meri", "mere", "kya", "kyun", "kab", "aap", "aapka", "ka", "ki", "ke", "ko", "haan", "ji", "paisa", "paise",
             "abhi", "band", "chalu", "mujhe", "chahiye", "karo", "kar", "do", "bhi", "se", "par", "tak", "wala", "wali", "kitna", "kitne", "aaya", "aayi",
             "hua", "hui", "gaya", "gayi", "bata", "batao", "dikha", "dikhao", "bhejo", "karna", "karni", "mil", "mila", "mile", "samajh"}
_LANG_BY_NAME = {"Hindi in Devanagari script": "hi-IN", "Kannada in Kannada script": "kn-IN", "English": "en-IN"}
_DEVANAGARI, _KANNADA = re.compile(r"[ऀ-ॿ]"), re.compile(r"[ಀ-೿]")

# Which fields of which system read are worth saying, in the order they are said. Anything not listed is never claimed: the table is the whole vocabulary.
CLAIM_FIELDS: dict[str, tuple[str, ...]] = {
    "get_device_status": ("status", "last_heartbeat", "signal_pct", "battery_pct"),
    "get_kyc_status": ("status", "reason"),
    "get_bank_status": ("status", "reason"),
    "get_plan": ("plan_id", "status", "monthly_fee_paise", "billing_date"),
    "get_risk_status": ("open_flags", "has_open_hold"),
    "check_partner_eligibility": ("eligible",),
}
# Reads whose body is a list of records: the newest record, and which of its fields to say.
LIST_FIELDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "get_settlements": ("settlements", "settlement_id", ("status", "amount_paise", "expected_at", "reason")),
    "get_transactions": ("payments", "payment_id", ("status", "amount_paise", "ts")),
}
_SUBJECT = {"get_device_status": "Device", "get_kyc_status": "KYC", "get_bank_status": "Bank", "get_plan": "Plan", "get_risk_status": "Risk",
            "check_partner_eligibility": "Loan eligibility", "get_settlements": "Settlement", "get_transactions": "Payment"}

# (playbook, step) -> the one move offline mode may propose. It is used only if the action is already in the guard's allow-list for this step, and only when
# ``when`` (a check on the system reads) holds. Arguments come from the table, never from the merchant's words.
MOVES: dict[tuple[str, str], tuple[str, dict, Any]] = {
    ("C2_SOUNDBOX_FAULT", "check_basics"): ("restart_device", {}, lambda b: b.get("get_device_status", {}).get("status") != "ONLINE"),
    ("C2_SOUNDBOX_FAULT", "remote_fix"): ("push_device_config", {}, lambda b: b.get("get_device_status", {}).get("status") != "ONLINE"),
    ("C1_SETTLEMENT_DELAY", "bank_fix"): ("retrigger_bank_verification", {}, lambda b: b.get("get_bank_status", {}).get("status") == "FAILED"),
    ("C5_DETAIL_UPDATE", "link"): ("send_bank_update_link", {}, None),
    ("S1_ACTIVATION", "kyc"): ("send_kyc_resume_link", {}, lambda b: b.get("get_kyc_status", {}).get("status") in ("REJECTED", "PENDING", "NOT_STARTED")),
    ("S1_ACTIVATION", "bank"): ("send_bank_update_link", {}, lambda b: b.get("get_bank_status", {}).get("status") == "FAILED"),
    ("S3_LOAN", "apply"): ("send_partner_application_link", {}, lambda b: b.get("check_partner_eligibility", {}).get("eligible") is True),
}

_SOURCE_LINE = re.compile(r"^\[(tool:([A-Za-z_]+)#r\d+)\] (\{.*\})\s*$", re.M)
_KB_LINE = re.compile(r"^\[(kb:[^\]\s]+)\] (.+)$", re.M)
_PLAYBOOK_LINE = re.compile(r"PLAYBOOK (\S+) .*?Current step: (\w+)")
_ACTION_LINE = re.compile(r"^- ([a-z_]+)\(", re.M)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


# ================================================================================================================ language
def detect_language(text: str) -> str:
    if _KANNADA.search(text):
        return "kn-IN"
    if _DEVANAGARI.search(text):
        return "hi-IN"
    words = re.findall(r"[a-z]+", text.lower())
    return "hinglish" if sum(w in _HINGLISH for w in words) >= 1 else "en-IN"


# ================================================================================================================ the Hinglish sentences
class Hinglish:
    """One English claim sentence -> one Hinglish sentence, and the exact reverse lookup the faithfulness check uses. The forward map is written to a shared
    table, so the reverse is exact for every sentence this process rendered and unknown for anything else (which the checker then fails closed)."""

    def __init__(self):
        self._back: dict[str, str] = {}

    def forward(self, english: str) -> str:
        m = re.match(r"^(?P<left>.+?) is (?P<value>.+)\.$", english.strip())
        if not m:
            out = english                                    # no safe template: say it in English rather than guess
        else:
            words = m["left"].split(" ")
            subject, label = words[0], " ".join(words[1:])
            out = f"{subject} ka {label} {m['value']} hai." if label else f"{subject} {m['value']} hai."
        self._back[out] = english
        return out

    def backward(self, text: str) -> str:
        return self._back.get(text.strip(), text)


class OfflineTranslator:
    """The ``Translator`` the faithfulness check expects. English is returned as is; Hinglish is looked up in what the renderer wrote."""

    def __init__(self, hinglish: Hinglish):
        self.hinglish = hinglish

    def to_english(self, text: str, language: str) -> str:
        return text if language == "en-IN" else self.hinglish.backward(text)


# ================================================================================================================ the provider
class OfflineProvider:
    name = "offline"
    model_id = MODEL_ID

    def __init__(self, hinglish: Hinglish | None = None):
        self.hinglish = hinglish or Hinglish()

    def chat(self, messages: list[dict], *, response_format: dict | None = None, **overrides) -> ChatResult:
        system = str(messages[0]["content"])
        user = str(messages[1]["content"]) if len(messages) > 1 else ""
        if system.startswith("You write the reply"):
            body = self._render(user)
        elif "turn agent" in system[:200]:
            body = self._decide(system, user)
        else:
            raise ValueError("offline mode does not know this prompt")
        text = json.dumps(body, ensure_ascii=False)
        return ChatResult(text, {"prompt_tokens": 0, "completion_tokens": 0}, {"offline": True})

    # -- decision ---------------------------------------------------------------------------------------------------
    def _decide(self, system: str, user: str) -> dict:
        message = ""
        if LATEST_OPEN in user and LATEST_CLOSE in user:
            message = user.split(LATEST_OPEN, 1)[1].split(LATEST_CLOSE, 1)[0].strip()
        language = detect_language(message)
        found = detect_intent(message, language_hint={"hinglish": "hinglish", "hi-IN": "hi", "kn-IN": "kn"}.get(language))
        intents = [{"intent": name, "w": min(1.0, float(score))} for name, score in found[:3]]

        bodies: dict[str, Any] = {}
        sources: list[tuple[str, str, dict]] = []
        for sid, read, raw in _SOURCE_LINE.findall(system):
            try:
                body = json.loads(raw)
            except ValueError:
                continue
            if isinstance(body, dict):
                bodies.setdefault(read, body)
                sources.append((sid, read, body))

        claims: list[dict] = []
        if found:
            for sid, read, body in sources:
                for text in self._claim_texts(read, body):
                    if len(claims) >= MAX_CLAIMS:
                        break
                    claims.append({"id": f"c{len(claims) + 1}", "text": text, "source": sid})
            if not claims:                                   # no system read speaks to it: an approved document might
                for sid, text in _KB_LINE.findall(system)[:1]:
                    first = _SENTENCE.split(text.strip())[0][:230].strip()
                    if first and not first.endswith(("?", "!")):
                        claims.append({"id": "c1", "text": first, "source": sid})

        action = None
        pb = _PLAYBOOK_LINE.search(system)
        if pb and found and claims:
            move = MOVES.get((pb.group(1), pb.group(2)))
            allowed = set(_ACTION_LINE.findall(system))
            if move and move[0] in allowed and (move[2] is None or move[2](bodies)):
                action = {"name": move[0], "args": dict(move[1])}
        return {"language": language, "intents": intents, "focus_case": None, "new_case_requests": [], "next_action": action,
                "claims_en": claims, "ask_clarification": not found}

    @staticmethod
    def _claim_texts(read: str, body: dict) -> list[str]:
        subject = _SUBJECT.get(read, _label(read.removeprefix("get_").removeprefix("check_")).capitalize())
        out: list[str] = []
        if read in CLAIM_FIELDS:
            for key in CLAIM_FIELDS[read]:
                if body.get(key) is not None:
                    out.append(f"{subject} {_label(key)} is {_fmt_value(key, body[key])}.")
        elif read in LIST_FIELDS:
            list_key, id_key, keys = LIST_FIELDS[read]
            rows = body.get(list_key) or []
            if rows and isinstance(rows[0], dict):
                row = rows[0]
                for key in keys:
                    if row.get(key) is not None:
                        out.append(f"{subject} {row.get(id_key, '')} {_label(key)} is {_fmt_value(key, row[key])}.".replace("  ", " "))
        return out

    # -- render -----------------------------------------------------------------------------------------------------
    def _render(self, user: str) -> dict:
        payload = json.loads(user)
        language = _LANG_BY_NAME.get(payload.get("language", ""), "hinglish" if str(payload.get("language", "")).startswith("Hinglish") else "en-IN")
        sentences = []
        for c in payload.get("claims", []):
            text = self.hinglish.forward(c["text"]) if language == "hinglish" else c["text"]
            sentences.append({"claim_ids": [c["id"]], "text": text})
        return {"sentences": sentences}


def offline_client(ledger=None, hinglish: Hinglish | None = None):
    """An ``LLMClient`` over the offline provider, so every model call is still ledgered as a CONTEXT_HASH entry whose model_id says ``offline-rules-v1``."""
    from core.llm.client import LLMClient
    return LLMClient(OfflineProvider(hinglish), None, ledger)
