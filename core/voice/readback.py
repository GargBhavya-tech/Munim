"""Read-back before action (#40): a write proposed on low-confidence speech is HELD until the merchant says yes.

The model's proposal (already through the decision guard) arrives with the voice turn's ``low_confidence`` flag. For a write action on a
low-confidence turn the gate stores the action as a PENDING confirmation and hands back a read-back prompt built by CODE from the args
(the number is formatted from the integer, never produced by a model). Then:
  * a short, unambiguous yes confirms it, once; the engine then submits the stored action through the executor as usual;
  * a no, or two unclear answers, drops it;
  * silence, or the sim clock passing ``expires_at``, expires it. Expiry NEVER confirms: reading back and executing anyway on timeout is
    the named mistake.
A reply that contains a different number than the one read back is unclear, not a yes ("haan, 3000 kar do" after "2000?").
Reads and drafts (R0/DRAFT) are not held: they change nothing.
Fixed phrases below are conversational templates, not claims. NATIVE-SPEAKER REVIEW EXPECTED for hi, kn and hinglish.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from core.actions.registry import ActionRegistry, RiskClass
from core.engine.clock import SimClock, iso
from core.engine.intents import normalise_text
from core.gate.numbers import extract_numbers, number_key
from core.memory.store import next_id, write_txn
from core.voice.pipeline import load_voice_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_confirmations (
  confirmation_id TEXT PRIMARY KEY, case_id TEXT, merchant_id TEXT NOT NULL, action TEXT NOT NULL, args_json TEXT NOT NULL,
  slot_text TEXT NOT NULL, language TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', unclear INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
"""

YES = frozenset({"haan", "haa", "han", "ha", "haanji", "ji", "yes", "yeah", "yep", "correct", "right", "sahi", "theek", "thik", "ok", "okay",
                 "हां", "हाँ", "हा", "जी", "सही", "ठीक", "ಹೌದು", "ಹೌದ", "ಸರಿ", "ಓಕೆ"})
NO = frozenset({"nahi", "nahin", "na", "no", "nope", "mat", "galat", "wrong", "नहीं", "नही", "ना", "मत", "गलत", "ಇಲ್ಲ", "ಬೇಡ", "ತಪ್ಪು"})
MAX_WORDS = 4

PROMPT = {
    "en-IN": "I heard {slot}. Is that right? Please say yes or no.",
    "hi-IN": "मैंने सुना: {slot}। क्या यह सही है? कृपया हाँ या नहीं बोलिए।",
    "hinglish": "Maine suna: {slot}. Kya yeh sahi hai? Kripya haan ya nahi boliye.",
    "kn-IN": "ನಾನು ಕೇಳಿದ್ದು: {slot}. ಇದು ಸರಿಯೇ? ದಯವಿಟ್ಟು ಹೌದು ಅಥವಾ ಇಲ್ಲ ಎಂದು ಹೇಳಿ.",
}
MONTHS = {"en-IN": "{n} month(s) of fee waiver", "hi-IN": "{n} महीने की फ़ीस माफ़ी", "hinglish": "{n} mahine ki fee maafi", "kn-IN": "{n} ತಿಂಗಳ ಶುಲ್ಕ ವಿನಾಯಿತಿ"}
GENERIC = {"en-IN": "this request", "hi-IN": "यह अनुरोध", "hinglish": "yeh request", "kn-IN": "ಈ ವಿನಂತಿ"}
LABELS = {
    "create_device_replacement": {"en-IN": "a device replacement", "hi-IN": "डिवाइस बदलना", "hinglish": "device replacement", "kn-IN": "ಸಾಧನ ಬದಲಾವಣೆ"},
    "create_device_order": {"en-IN": "a device order", "hi-IN": "डिवाइस का ऑर्डर", "hinglish": "device ka order", "kn-IN": "ಸಾಧನ ಆರ್ಡರ್"},
    "create_closure_request": {"en-IN": "closing the account", "hi-IN": "खाता बंद करना", "hinglish": "account band karna", "kn-IN": "ಖಾತೆ ಮುಚ್ಚುವುದು"},
}


def format_rupees(paise: int) -> str:
    rupees, rem = divmod(int(paise), 100)
    s = f"{rupees:,}"
    return f"₹{s}" if rem == 0 else f"₹{s}.{rem:02d}"


def slot_text(action: str, args: dict, language: str) -> str:
    """The key slot as the merchant will hear it, formatted by code from the system-validated args."""
    if "amount_paise" in args:
        return format_rupees(args["amount_paise"])
    if "months" in args:
        return MONTHS.get(language, MONTHS["en-IN"]).format(n=int(args["months"]))
    return LABELS.get(action, {}).get(language) or GENERIC.get(language, GENERIC["en-IN"])


@dataclass(frozen=True)
class Hold:
    outcome: str                      # PROCEED | HOLD
    confirmation_id: str | None = None
    prompt: str | None = None
    slot: str | None = None


@dataclass(frozen=True)
class Resolution:
    status: str                       # CONFIRMED | DECLINED | UNCLEAR | NONE
    confirmation_id: str | None = None
    action: str | None = None         # set only for CONFIRMED
    args: dict | None = None


class ReadBackGate:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, registry: ActionRegistry, *, config: dict | None = None, ledger=None):
        self.conn, self.clock, self.registry, self.ledger = conn, clock, registry, ledger
        cfg = config if config is not None else load_voice_config()
        self.ttl = timedelta(minutes=float(cfg["readback_ttl_minutes"]))
        self.max_unclear = int(cfg["readback_max_unclear"])
        self.conn.executescript(SCHEMA)

    # -- hold ---------------------------------------------------------------------------------------------------------
    def review(self, case_id: str | None, merchant_id: str, action: str, args: dict, *, low_confidence: bool, language: str = "hinglish") -> Hold:
        if not low_confidence:
            return Hold("PROCEED")
        spec = self.registry.get(action)
        if spec.cls in (RiskClass.R0, RiskClass.DRAFT):
            return Hold("PROCEED")
        self.expire_due()
        slot = slot_text(action, args, language)
        now = self.clock.now()
        with write_txn(self.conn):
            self.conn.execute("UPDATE voice_confirmations SET status='SUPERSEDED' WHERE case_id IS ? AND merchant_id = ? AND status = 'PENDING'", (case_id, merchant_id))
            cid = next_id(self.conn, "VC")
            self.conn.execute("INSERT INTO voice_confirmations(confirmation_id, case_id, merchant_id, action, args_json, slot_text, language, created_at, expires_at)"
                              " VALUES (?,?,?,?,?,?,?,?,?)",
                              (cid, case_id, merchant_id, action, json.dumps(args, sort_keys=True), slot, language, iso(now), iso(now + self.ttl)))
            self._note(case_id, cid, "HELD", action)
        return Hold("HOLD", cid, PROMPT.get(language, PROMPT["en-IN"]).format(slot=slot), slot)

    # -- answer -------------------------------------------------------------------------------------------------------
    def resolve(self, case_id: str | None, merchant_id: str, text: str) -> Resolution:
        self.expire_due()
        row = self.conn.execute("SELECT * FROM voice_confirmations WHERE case_id IS ? AND merchant_id = ? AND status = 'PENDING' ORDER BY confirmation_id DESC LIMIT 1",
                                (case_id, merchant_id)).fetchone()
        if row is None:
            return Resolution("NONE")
        verdict = self._classify(text, row["slot_text"])
        cid = row["confirmation_id"]
        with write_txn(self.conn):
            if verdict == "YES":
                self.conn.execute("UPDATE voice_confirmations SET status='CONFIRMED' WHERE confirmation_id = ?", (cid,))
                self._note(case_id, cid, "CONFIRMED", row["action"])
                return Resolution("CONFIRMED", cid, row["action"], json.loads(row["args_json"]))
            if verdict == "NO":
                self.conn.execute("UPDATE voice_confirmations SET status='DECLINED' WHERE confirmation_id = ?", (cid,))
                self._note(case_id, cid, "DECLINED", row["action"])
                return Resolution("DECLINED", cid)
            unclear = row["unclear"] + 1
            if unclear >= self.max_unclear:
                self.conn.execute("UPDATE voice_confirmations SET status='DECLINED', unclear=? WHERE confirmation_id = ?", (unclear, cid))
                self._note(case_id, cid, "DECLINED", row["action"])
                return Resolution("DECLINED", cid)
            self.conn.execute("UPDATE voice_confirmations SET unclear=? WHERE confirmation_id = ?", (unclear, cid))
        return Resolution("UNCLEAR", cid)

    def expire_due(self) -> int:
        now = iso(self.clock.now())
        rows = self.conn.execute("SELECT confirmation_id, case_id, action FROM voice_confirmations WHERE status='PENDING' AND expires_at <= ?", (now,)).fetchall()
        if rows:
            with write_txn(self.conn):
                for r in rows:
                    self.conn.execute("UPDATE voice_confirmations SET status='EXPIRED' WHERE confirmation_id = ?", (r["confirmation_id"],))
                    self._note(r["case_id"], r["confirmation_id"], "EXPIRED", r["action"])
        return len(rows)

    def pending(self, case_id: str | None, merchant_id: str) -> bool:
        self.expire_due()
        return self.conn.execute("SELECT 1 FROM voice_confirmations WHERE case_id IS ? AND merchant_id = ? AND status='PENDING'", (case_id, merchant_id)).fetchone() is not None

    # -- internals ----------------------------------------------------------------------------------------------------
    @staticmethod
    def _classify(text: str, slot: str) -> str:
        words = normalise_text(text).split()
        if not words:
            return "UNCLEAR"
        said = {number_key(n) for n in extract_numbers(text)}
        if said and not said <= {number_key(n) for n in extract_numbers(slot)}:
            return "UNCLEAR"                      # a different number (digits or spoken words) is a new request, not a yes
        yes, no = any(w in YES for w in words), any(w in NO for w in words)
        if yes and no:
            return "UNCLEAR"
        if len(words) > MAX_WORDS:
            return "UNCLEAR"
        return "YES" if yes else ("NO" if no else "UNCLEAR")

    def _note(self, case_id: str | None, cid: str, status: str, action: str) -> None:
        if self.ledger is not None:
            self.ledger.append(kind="STATE", actor="SYSTEM", case_id=case_id, payload={"voice_confirmation": cid, "status": status, "action": action})
