"""Scam Shield (C10, #86; Bible section 23): is the customer's "payment done" screen real? Decided by CODE from the merchant's own
transactions, never by the image.

    window    claim time +-10 min, or the last 15 min when no time was read                         [ASSUMPTION]
    MATCH     one SUCCESS payment of exactly the claimed paise in the window (and the same reference, if one was read)
    PENDING   the only candidate is PENDING: "not received yet, wait for the Soundbox"
    NO_MATCH  nothing of that amount in the window (a made-up reference is simply not found)
    MISMATCH  the reference exists but the amount differs, or it belongs to ANOTHER merchant: fraud flag, a person takes it (P0)
    AMBIGUOUS the amount was not read confidently, or several payments fit: ask the merchant; never guess
A fraud-call report ("someone from Paytm asked for my OTP") gets the safety warning and a FRAUD_CALL flag.

Every number the merchant is told comes from the payment record the system returned (the amount of the MATCH/PENDING payment), never from
the screenshot. Reply texts below are fixed templates. NATIVE-SPEAKER REVIEW EXPECTED for hi-IN, hinglish and kn-IN.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.engine.clock import parse_iso
from core.engine.workflow import Decision

PLAYBOOK_ID = "C10_SCAM_SHIELD"
WINDOW = timedelta(minutes=10)          # [ASSUMPTION] Bible section 23
NO_TIME_WINDOW = timedelta(minutes=15)  # [ASSUMPTION]
RECHECK_HOURS = 0.25                    # a NO_MATCH / PENDING verdict is re-checked once, 15 minutes later
FRAUD_CALL_INTENT = "REPORT_FRAUD_CALL"
VERIFY_INTENT = "VERIFY_PAYMENT"
MATCH, PENDING, NO_MATCH, MISMATCH, AMBIGUOUS = "MATCH", "PENDING", "NO_MATCH", "MISMATCH", "AMBIGUOUS"


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str
    payment: dict | None = None
    candidates: int = 0
    sources: dict = field(default_factory=dict)      # source id -> the system body the verdict rests on


def _claim_time(draft: dict, now: datetime) -> datetime | None:
    hhmm = draft.get("time_hhmm")
    if not hhmm:
        return None
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t - timedelta(days=1) if t - now > timedelta(hours=1) else t      # a 23:58 screen shown at 00:03 is yesterday's


def verify(reader, merchant_id: str, draft: dict, now: datetime) -> Verdict:
    """``draft``: the DRAFT reading's payload (amount_paise, time_hhmm, reference, ...), unread fields None."""
    amount, ref = draft.get("amount_paise"), draft.get("reference")
    tx = reader("get_transactions", {"merchant_id": merchant_id, "limit": 100})
    sources = {"tool:get_transactions#scam": tx}
    if ref:
        found = reader("get_payment_by_reference", {"merchant_id": merchant_id, "reference": ref})
        sources["tool:get_payment_by_reference#scam"] = found
        if found["exists"] and not found["belongs_to_merchant"]:
            return Verdict(MISMATCH, "the reference belongs to another merchant's payment", None, 0, sources)
        if found["exists"]:
            p = found["payment"]
            if amount is not None and p["amount_paise"] != amount:
                return Verdict(MISMATCH, "the reference is this merchant's but for a different amount", p, 1, sources)
            if amount is None:
                return Verdict(AMBIGUOUS, "the reference was read but the amount was not", None, 1, sources)
            return _by_status(p, sources, "the reference and amount match")
    if amount is None:
        return Verdict(AMBIGUOUS, "the amount could not be read confidently", None, 0, sources)
    t = _claim_time(draft, now)
    lo, hi = (t - WINDOW, t + WINDOW) if t else (now - NO_TIME_WINDOW, now)
    cands = [p for p in tx.get("payments") or [] if lo <= parse_iso(p["ts"]) <= hi and p["amount_paise"] == amount]
    ok = [p for p in cands if p["status"] == "SUCCESS"]
    if len(ok) == 1:
        return Verdict(MATCH, "one successful payment of that amount in the window", ok[0], 1, sources)
    if len(ok) > 1:
        return Verdict(AMBIGUOUS, f"{len(ok)} payments of that amount in the window", None, len(ok), sources)
    pend = [p for p in cands if p["status"] == "PENDING"]
    if pend:
        return Verdict(PENDING, "a payment of that amount is still pending", pend[0], len(pend), sources)
    return Verdict(NO_MATCH, "no payment of that amount in the window", None, 0, sources)


def _by_status(p: dict, sources: dict, why: str) -> Verdict:
    if p["status"] == "SUCCESS":
        return Verdict(MATCH, why, p, 1, sources)
    if p["status"] == "PENDING":
        return Verdict(PENDING, why + " but it is still pending", p, 1, sources)
    return Verdict(NO_MATCH, why + " but the payment FAILED", p, 1, sources)


# ------------------------------------------------------------------------------------------------------------------- replies
def rupees(paise: int) -> str:
    r, rem = divmod(int(paise), 100)
    return f"₹{r:,}" + (f".{rem:02d}" if rem else "")


def _hhmm(ts: str) -> str:
    return parse_iso(ts).strftime("%H:%M")


TEXT = {
    MATCH: {"en-IN": "Payment of {amount} received at {time}. You can hand over the goods.",
            "hinglish": "{amount} ka payment {time} baje aa gaya hai. Aap saamaan de sakte hain.",
            "hi-IN": "{amount} का पेमेंट {time} बजे आ गया है। आप सामान दे सकते हैं।",
            "kn-IN": "{amount} ಪೇಮೆಂಟ್ {time} ಕ್ಕೆ ಬಂದಿದೆ. ನೀವು ಸಾಮಾನು ಕೊಡಬಹುದು."},
    PENDING: {"en-IN": "A payment of {amount} is still pending and has not reached you yet. Wait for the Soundbox announcement before handing over the goods.",
              "hinglish": "{amount} ka payment abhi pending hai, aapke paas nahi aaya. Soundbox ki awaaz aane tak saamaan mat dijiye.",
              "hi-IN": "{amount} का पेमेंट अभी पेंडिंग है, आपके पास नहीं आया। साउंडबॉक्स की आवाज़ आने तक सामान मत दीजिए।",
              "kn-IN": "{amount} ಪೇಮೆಂಟ್ ಇನ್ನೂ ಪೆಂಡಿಂಗ್ ಇದೆ, ನಿಮಗೆ ಬಂದಿಲ್ಲ. ಸೌಂಡ್‌ಬಾಕ್ಸ್ ಹೇಳುವವರೆಗೆ ಸಾಮಾನು ಕೊಡಬೇಡಿ."},
    NO_MATCH: {"en-IN": "No matching payment has reached your account in this time. Do not hand over the goods yet. I will check once more in 15 minutes.",
               "hinglish": "Is samay mein aapke account mein aisa koi payment nahi aaya. Abhi saamaan mat dijiye. Main 15 minute baad ek baar phir check karunga.",
               "hi-IN": "इस समय में आपके खाते में ऐसा कोई पेमेंट नहीं आया। अभी सामान मत दीजिए। मैं 15 मिनट बाद एक बार फिर जाँच करूँगा।",
               "kn-IN": "ಈ ಸಮಯದಲ್ಲಿ ನಿಮ್ಮ ಖಾತೆಗೆ ಅಂತಹ ಯಾವುದೇ ಪೇಮೆಂಟ್ ಬಂದಿಲ್ಲ. ಈಗ ಸಾಮಾನು ಕೊಡಬೇಡಿ. 15 ನಿಮಿಷದ ನಂತರ ಮತ್ತೊಮ್ಮೆ ಪರಿಶೀಲಿಸುತ್ತೇನೆ."},
    MISMATCH: {"en-IN": "Warning: this payment screen does not match your account's records. Do not hand over the goods. I have alerted our risk team and a colleague will contact you.",
               "hinglish": "Savdhaan: yeh payment screen aapke account ke record se match nahi karti. Saamaan mat dijiye. Maine risk team ko bata diya hai, ek colleague aapse sampark karenge.",
               "hi-IN": "सावधान: यह पेमेंट स्क्रीन आपके खाते के रिकॉर्ड से मेल नहीं खाती। सामान मत दीजिए। मैंने रिस्क टीम को बता दिया है, एक सहकर्मी आपसे संपर्क करेंगे।",
               "kn-IN": "ಎಚ್ಚರಿಕೆ: ಈ ಪೇಮೆಂಟ್ ಸ್ಕ್ರೀನ್ ನಿಮ್ಮ ಖಾತೆಯ ದಾಖಲೆಗೆ ಹೊಂದುತ್ತಿಲ್ಲ. ಸಾಮಾನು ಕೊಡಬೇಡಿ. ರಿಸ್ಕ್ ತಂಡಕ್ಕೆ ತಿಳಿಸಿದ್ದೇನೆ, ಸಹೋದ್ಯೋಗಿ ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತಾರೆ."},
    AMBIGUOUS: {"en-IN": "I could not match this clearly. Please tell me the amount and the time of the payment.",
                "hinglish": "Main ise saaf match nahi kar paaya. Kripya payment ki rakam aur samay bataiye.",
                "hi-IN": "मैं इसे साफ़ मिला नहीं पाया। कृपया पेमेंट की रकम और समय बताइए।",
                "kn-IN": "ಇದನ್ನು ಸ್ಪಷ್ಟವಾಗಿ ಹೊಂದಿಸಲು ಆಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಪೇಮೆಂಟ್ ಮೊತ್ತ ಮತ್ತು ಸಮಯ ತಿಳಿಸಿ."},
    FRAUD_CALL_INTENT: {"en-IN": "Paytm never asks for your PIN or OTP. Do not share them with anyone. I have reported this call to our risk team.",
                        "hinglish": "Paytm kabhi aapka PIN ya OTP nahi maangta. Kisi ko mat bataiye. Maine is call ki report risk team ko kar di hai.",
                        "hi-IN": "पेटीएम कभी आपका पिन या ओटीपी नहीं मांगता। किसी को मत बताइए। मैंने इस कॉल की रिपोर्ट रिस्क टीम को कर दी है।",
                        "kn-IN": "ಪೇಟಿಎಂ ಎಂದಿಗೂ ನಿಮ್ಮ ಪಿನ್ ಅಥವಾ ಒಟಿಪಿ ಕೇಳುವುದಿಲ್ಲ. ಯಾರಿಗೂ ಹೇಳಬೇಡಿ. ಈ ಕರೆಯ ಬಗ್ಗೆ ರಿಸ್ಕ್ ತಂಡಕ್ಕೆ ವರದಿ ಮಾಡಿದ್ದೇನೆ."},
    "RECHECK_MATCH": {"en-IN": "Update: the payment of {amount} has now arrived, at {time}.",
                      "hinglish": "Update: {amount} ka payment ab {time} baje aa gaya hai.",
                      "hi-IN": "अपडेट: {amount} का पेमेंट अब {time} बजे आ गया है।",
                      "kn-IN": "ಅಪ್‌ಡೇಟ್: {amount} ಪೇಮೆಂಟ್ ಈಗ {time} ಕ್ಕೆ ಬಂದಿದೆ."},
}


def reply(status: str, language: str, payment: dict | None = None) -> str:
    lang = language if language in TEXT[MATCH] else "en-IN"
    t = TEXT[status][lang]
    if payment is not None:
        return t.format(amount=rupees(payment["amount_paise"]), time=_hhmm(payment["ts"]))
    return t


# ------------------------------------------------------------------------------------------------------------------- the handler
def text_draft(text: str) -> dict:
    """"did 2000 come?": the amount the merchant typed is a DRAFT claim like a screenshot field (time unknown -> last 15 minutes)."""
    from core.gate.numbers import extract_numbers
    nums = [n for n in extract_numbers(text or "") if n.kind in ("money", "plain", "count")]
    paise = {int(n.value) if n.kind == "money" else int(n.units * 100) for n in nums if n.kind == "money" or n.units is not None}
    amt = paise.pop() if len(paise) == 1 else None      # two different amounts ("2000 or 200?") is not a claim: ask instead
    return {"amount_paise": amt, "time_hhmm": None, "reference": None, "app": None, "confidence": {}, "source": "text"}


def _last_draft(conn, case_id: str) -> dict | None:
    for r in conn.execute("SELECT e.payload_json FROM case_events ce JOIN events e ON e.event_id = ce.event_id WHERE ce.case_id = ? "
                          "AND e.type = 'MERCHANT_MESSAGE' ORDER BY e.seq DESC", (case_id,)):
        p = json.loads(r["payload_json"])
        if p.get("screenshot"):
            return p["screenshot"]
        if p.get("text"):
            d = text_draft(p["text"])
            if d["amount_paise"]:
                return d
    return None


def make_handler(conn, reader, clock, ledger=None):
    """The C10 decider: code only. ``handler(ctx, language) -> Decision``. Works as a scripted-table entry and as a TurnAgent handler."""
    def handler(ctx, language: str = "en-IN") -> Decision:
        case, ev = ctx.case, ctx.event
        mid = case.merchant_id
        p = ev.payload or {}
        intent = p.get("intent")
        if ev.type == "MERCHANT_MESSAGE" and intent == FRAUD_CALL_INTENT:
            _note(ledger, case.case_id, FRAUD_CALL_INTENT, "fraud call reported")
            return Decision(action=("flag_fraud", {"merchant_id": mid, "kind": "FRAUD_CALL", "detail": "merchant reported a call asking for PIN/OTP"}),
                            message_en=reply(FRAUD_CALL_INTENT, language), language=language)
        if ev.type == "MERCHANT_MESSAGE":
            draft = p.get("screenshot") or text_draft(p.get("text", ""))
            v = verify(reader, mid, draft, clock.now())
            _note(ledger, case.case_id, v.status, v.reason)
            if v.status == MISMATCH:
                return Decision(action=("flag_fraud", {"merchant_id": mid, "kind": "REFERENCE_MISMATCH" if draft.get("reference") else "FAKE_PAYMENT_SCREENSHOT",
                                                       "detail": v.reason}),
                                message_en=reply(MISMATCH, language), language=language, escalate=[f"FRAUD_SIGNAL: {v.reason}"])
            follow = RECHECK_HOURS if v.status in (NO_MATCH, PENDING) else None
            return Decision(message_en=reply(v.status, language, v.payment), language=language, follow_up_hours=follow)
        if ev.type in ("TIMER_FIRED", "PAYMENT_SUCCESS"):              # the one re-check of a NO_MATCH / PENDING verdict
            draft = _last_draft(conn, case.case_id)
            if draft is None:
                return Decision()
            v = verify(reader, mid, draft, clock.now())
            if v.status == MATCH and last_verdict(conn, case.case_id) in (NO_MATCH, PENDING):
                _note(ledger, case.case_id, "RECHECK_MATCH", v.reason)
                return Decision(message_en=reply("RECHECK_MATCH", language, v.payment), language=language, answers_merchant=True)
        return Decision()
    return handler


def _note(ledger, case_id: str, status: str, reason: str) -> None:
    if ledger is not None:
        ledger.append(kind="DECISION", actor="SYSTEM", case_id=case_id, payload={"scam_verdict": status, "reason": reason, "decided_by": "code"})


def last_verdict(conn, case_id: str) -> str | None:
    for r in conn.execute("SELECT payload_json FROM ledger WHERE case_id = ? AND kind = 'DECISION' ORDER BY seq DESC", (case_id,)):
        v = json.loads(r["payload_json"]).get("scam_verdict")
        if v:
            return v
    return None
