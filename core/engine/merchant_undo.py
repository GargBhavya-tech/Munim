"""Merchant undo (#65): "wapas karo" / "undo that" -> UNDO_LAST -> the compensation of THIS case's last undoable action.

Code handles it, not the model: the intent is a control intent, the action is found by a query scoped to the one case, and the
compensation runs through the executor's own undo (same pipeline, window enforced, ledgered as COMPENSATION). Another case's action is
never reachable from here: the query filters on case_id. Receipts are the same query's open rows, shown to the merchant as cards.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.actions.types import ActionOutcome, Principal
from core.engine.clock import parse_iso

MERCHANT_REQUEST = Principal("SYSTEM", "merchant_request")   # code acts on the merchant's words; compensations are never agent-callable
OPEN_ENDED = "9999-"                       # compensable_until of an R2 that stays undoable while its object is open

LABELS = {                                 # NATIVE-SPEAKER REVIEW EXPECTED for hi, hinglish and kn
    "create_device_replacement": {"en-IN": "Replacement ordered", "hi-IN": "डिवाइस बदलने का ऑर्डर हो गया", "hinglish": "Replacement order ho gaya",
                                  "kn-IN": "ಬದಲಿ ಸಾಧನ ಆರ್ಡರ್ ಆಗಿದೆ"},
    "create_device_order": {"en-IN": "Device ordered", "hi-IN": "डिवाइस का ऑर्डर हो गया", "hinglish": "Device order ho gaya", "kn-IN": "ಸಾಧನ ಆರ್ಡರ್ ಆಗಿದೆ"},
    "schedule_follow_up": {"en-IN": "Follow-up scheduled", "hi-IN": "फ़ॉलो-अप तय हो गया", "hinglish": "Follow-up set ho gaya", "kn-IN": "ಫಾಲೋ-ಅಪ್ ನಿಗದಿಯಾಗಿದೆ"},
    "raise_ticket": {"en-IN": "Ticket raised", "hi-IN": "टिकट दर्ज हो गया", "hinglish": "Ticket raise ho gaya", "kn-IN": "ಟಿಕೆಟ್ ದಾಖಲಾಗಿದೆ"},
}
CANCEL_UNTIL = {"en-IN": "cancel until {t}", "hi-IN": "{t} तक रद्द कर सकते हैं", "hinglish": "{t} tak cancel kar sakte hain", "kn-IN": "{t} ವರೆಗೆ ರದ್ದು ಮಾಡಬಹುದು"}
CANCEL_OPEN = {"en-IN": "cancel any time while it is open", "hi-IN": "खुला रहने तक रद्द कर सकते हैं", "hinglish": "khula rehne tak cancel kar sakte hain",
               "kn-IN": "ತೆರೆದಿರುವವರೆಗೆ ರದ್ದು ಮಾಡಬಹುದು"}
DONE = {"en-IN": "Done. I have cancelled it: {label}.", "hi-IN": "हो गया। मैंने इसे रद्द कर दिया: {label}।", "hinglish": "Ho gaya. Maine ise cancel kar diya: {label}.",
        "kn-IN": "ಆಯಿತು. ಇದನ್ನು ರದ್ದು ಮಾಡಿದ್ದೇನೆ: {label}."}
NOTHING = {"en-IN": "There is nothing I can cancel for this request now.", "hi-IN": "इस अनुरोध में अभी रद्द करने के लिए कुछ नहीं है।",
           "hinglish": "Is request mein abhi cancel karne ke liye kuch nahi hai.", "kn-IN": "ಈ ವಿನಂತಿಯಲ್ಲಿ ಈಗ ರದ್ದು ಮಾಡಲು ಏನೂ ಇಲ್ಲ."}


@dataclass(frozen=True)
class Receipt:
    action_id: str
    action: str
    label: str
    cancel: str                            # "cancel until 6:00 PM" or the open-ended wording
    compensable_until: str | None


def _label(action: str, language: str) -> str:
    return LABELS.get(action, {}).get(language) or LABELS.get(action, {}).get("en-IN") or action.replace("_", " ").capitalize()


def _clock(iso_ts: str) -> str:
    t = parse_iso(iso_ts)
    return t.strftime("%I:%M %p").lstrip("0")


def open_undoable(conn: sqlite3.Connection, clock, case_id: str) -> list[sqlite3.Row]:
    """This case's actions whose compensation is still OPEN and inside its window, newest first."""
    now = clock.now()
    rows = conn.execute("SELECT a.action_id, a.name, c.compensable_until FROM compensations c JOIN actions a ON a.action_id = c.action_id "
                        "WHERE a.case_id = ? AND a.status = 'OK' AND c.status = 'OPEN' ORDER BY a.rowid DESC", (case_id,)).fetchall()
    return [r for r in rows if r["compensable_until"] and (r["compensable_until"].startswith(OPEN_ENDED) or parse_iso(r["compensable_until"]) >= now)]


def receipts(conn: sqlite3.Connection, clock, case_id: str, language: str = "en-IN") -> list[Receipt]:
    out = []
    for r in open_undoable(conn, clock, case_id):
        cu = r["compensable_until"]
        open_ended = cu.startswith(OPEN_ENDED)
        lang = language if language in CANCEL_UNTIL else "en-IN"
        cancel = CANCEL_OPEN[lang] if open_ended else CANCEL_UNTIL[lang].format(t=_clock(cu))
        out.append(Receipt(r["action_id"], r["name"], _label(r["name"], lang), cancel, None if open_ended else cu))
    return out


def undo_last(executor, conn: sqlite3.Connection, clock, case_id: str) -> tuple[ActionOutcome | None, str | None]:
    """(outcome, undone action name). (None, None) when this case has nothing undoable."""
    rows = open_undoable(conn, clock, case_id)
    if not rows:
        return None, None
    return executor.undo(rows[0]["action_id"], MERCHANT_REQUEST), rows[0]["name"]


def reply(language: str, undone_action: str | None, ok: bool) -> str:
    lang = language if language in DONE else "en-IN"
    if undone_action and ok:
        return DONE[lang].format(label=_label(undone_action, lang))
    return NOTHING[lang]


__all__ = ["Receipt", "open_undoable", "receipts", "undo_last", "reply"]
