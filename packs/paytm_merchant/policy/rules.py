"""The sixteen Paytm-merchant policy rules (Bible section 30). Every number comes from limits.yaml.

Rule inputs: the intent, intent.facts (engine-known case facts), live state through the Reader / DB, limits.
WAIVER-BAND-001 reads the band from the pricing system (reader 'get_offer_band'), never from the intent.

Facts keys read: closure_requests, kfs_message_sent, mandate_month_total_paise, mandate_limit_paise, mandate_purpose,
targeting, via_mcp, analysis, merchant_id, settlement_id, lead_id, subject_id.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterable

from core.actions.registry import FORBIDDEN_ACTIONS, RiskClass
from core.engine import roles as roles_mod
from core.engine.policy import Ctx, Finding, PolicyEngine, Rule, load_limits

LIMITS_PATH = Path(__file__).with_name("limits.yaml")
WRITE = (RiskClass.R1, RiskClass.R2, RiskClass.R3)
HUMAN_ONLY_KYC_ACTIONS = frozenset({"approve_kyc", "change_bank_details", "change_settlement_account", "accept_documents_in_chat"})
SECRET_KEYS = frozenset({"otp", "pin", "mpin", "upi_pin", "cvv", "password", "passcode", "atm_pin"})
SENSITIVE_KEYS = ("account_number", "account_no", "acct", "ifsc", "aadhaar", "document", "pan_number")
ID_KEY_SUFFIXES = ("_id", "_at", "reference", "ref")
LOAN_TEXT_ACTIONS = ("send_message", "draft_message")


# -- helpers --------------------------------------------------------------------------------------------
def normalise_digits(s: str) -> str:
    """Map any Unicode decimal digit (Devanagari, Bengali, fullwidth ...) to ASCII so '१२३४' is read as 1234."""
    s = unicodedata.normalize("NFKC", s)
    return "".join(str(unicodedata.decimal(ch)) if ch.isdigit() and unicodedata.decimal(ch, None) is not None else ch for ch in s)


def _walk(obj, key=""):
    """Yield (key, value) for every leaf of args, with keys of nested dicts."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v, key)
    else:
        yield key, obj


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k).lower()
            yield from _keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _keys(v)


def _texts(args: dict, *, skip_ids: bool = True) -> list[str]:
    out = []
    for k, v in _walk(args):
        if isinstance(v, str) and not (skip_ids and k.lower().endswith(ID_KEY_SUFFIXES)):
            out.append(normalise_digits(v))
    return out


def _is_contact(ctx: Ctx) -> bool:
    return ctx.spec is not None and ctx.spec.contact


def _channel(ctx: Ctx) -> str | None:
    return ctx.intent.channel or ctx.args.get("channel")


def _consent_granted(ctx: Ctx, subject: str, channel: str) -> bool:
    row = ctx.conn.execute("SELECT status FROM consent WHERE subject_id = ? AND channel = ?", (subject, channel)).fetchone()
    return bool(row) and row["status"] == ctx.limits["consent"]["granted_status"]


def _is_human(ctx: Ctx) -> bool:
    return ctx.intent.principal.kind == "HUMAN"


def waiver_ceiling(ctx: Ctx, band: int) -> int:
    """Months this principal may reach without a human decision: the band, or for a human the role's cap if higher."""
    if _is_human(ctx):
        return max(band, ctx.limits["waiver"]["role_cap_months"].get(ctx.intent.principal.role, 0))
    return band


# -- rules ----------------------------------------------------------------------------------------------
def fix_first(ctx: Ctx):
    """No offer action while a P0/P1 case is open for the merchant (lanes come from the engine, intent.open_case_lanes)."""
    if ctx.spec is None or not ctx.spec.offer:
        return None
    blocking = set(ctx.limits["fix_first"]["blocking_lanes"])
    hit = sorted(blocking.intersection(ctx.intent.open_case_lanes))
    if hit:
        return Finding(f"offer action {ctx.intent.action!r} while a {'/'.join(hit)} case is open: fix the problem first")
    return None


def _nfkc(x: str) -> str:
    return unicodedata.normalize("NFKC", x)


def _fold(t: str) -> str:
    """Matching form of a text: digits normalised, zero-width joiners removed, lower-cased (Latin only; Indic scripts have no case)."""
    return t.replace("\u200c", "").replace("\u200d", "").lower()


def _alt(words) -> str:
    """Regex alternation of words. Latin words get \\b edges; Devanagari/Kannada words get none (\\b is unreliable next to matras)."""
    parts = []
    for w in words:
        w = _nfkc(w)
        parts.append(rf"\b{re.escape(w)}\b" if w.isascii() else re.escape(w))
    return "(?:" + "|".join(parts) + ")"


# Loan wording beyond what limits.yaml lists (Latin loan words live there). Hindi and Kannada script plus Hinglish spellings.
# NATIVE REVIEW: every Devanagari/Kannada term below was chosen from common usage and needs a native-speaker check.
LOAN_CONTEXT_EXTRA = ("karz", "karza", "byaj", "vyaj", "udhaar", "udhar", "kist", "kisht",
                      "लोन", "कर्ज", "कर्ज़", "ऋण", "ब्याज", "ईएमआई", "किस्त", "उधार", "क्रेडिट",
                      "ಸಾಲ", "ಲೋನ್", "ಬಡ್ಡಿ", "ಇಎಂಐ", "ಕಂತು", "ಕ್ರೆಡಿಟ್")
LOAN_CLAIM_EXTRA = ("approve", "manzoor", "manjoor", "pakka", "pakki",
                    "गारंटी", "गारण्टी", "गारंटीड", "स्वीकृत", "मंज़ूर", "मंजूर", "अप्रूव", "अप्रूव्ड", "पक्का",
                    "ಗ್ಯಾರಂಟಿ", "ಖಾತರಿ", "ಮಂಜೂರು", "ಅನುಮೋದನೆ", "ಅನುಮೋದಿತ", "ಅಪ್ರೂವ್", "ಖಚಿತ")
_NUM = r"[\d,]+(?:\.\d+)?"
_PCT = re.compile(r"\d+(?:\.\d+)?\s*%|\bper\s*cent\b|\bpercent\b|\bp\.?a\.?\b|\bapr\b|\broi\b|\bf(?:ee|i)sad[ia]\b|\bpratishat\b|"
                  + "|".join(re.escape(_nfkc(w)) for w in ("प्रतिशत", "फीसदी", "फ़ीसदी", "ಶೇಕಡಾ", "ಪ್ರತಿಶತ", "ಶೇ")), re.I)
_TENURE = re.compile(r"\b\d+\s*(?:months?|yrs?|years?|mahine|saal|din|days?)\b|\d+\s*(?:"
                     + "|".join(re.escape(_nfkc(w)) for w in ("महीने", "महीना", "माह", "साल", "वर्ष", "दिन", "ತಿಂಗಳು", "ತಿಂಗಳ", "ವರ್ಷ", "ದಿನ")) + ")", re.I)
_RUPEES = re.compile(rf"(?:₹|\brs\.?|\binr)\s*(?P<a>{_NUM})(?P<al>\s*(?:lakh|lac|लाख|ಲಕ್ಷ))?"
                     rf"|(?P<b>{_NUM})\s*(?P<bl>lakh|lac|लाख|ಲಕ್ಷ)"
                     rf"|(?P<c>{_NUM})\s*(?:rupees|rupaye|rupay|रुपये|रुपए|रुपया|ರೂಪಾಯಿ)", re.I)


# SECRET-001 vocabulary. Latin words keep \\b edges; Devanagari/Kannada words are matched bare. "PIN code" (an address) is not a secret.
# NATIVE REVIEW: the Devanagari/Kannada words and verb forms below are from common usage and need a native-speaker check.
SECRET_WORD = ("(?:\\b(?:otp|m?pin(?!\\s*code)|upi\\s*pin|cvv|passcode|password)\\b|"
               + "(?:" + "|".join(re.escape(_nfkc(x)) for x in ("ओटीपी", "ओ.टी.पी", "एमपिन", "सीवीवी", "पासवर्ड", "पासकोड")) + ")|"
               + "(?:पिन|ಪಿನ್)(?!\\s*(?:कोड|ಕೋಡ್))|"
               + "(?:" + "|".join(re.escape(_nfkc(x)) for x in ("ಒಟಿಪಿ", "ಎಂಪಿನ್", "ಸಿವಿವಿ", "ಪಾಸ್ವರ್ಡ್", "ಪಾಸ್\u200cವರ್ಡ್", "ಪಾಸ್ ವರ್ಡ್")) + "))")
_SECRET_VERBS_LATIN = ("share", "send", "tell", "give", "enter", "type", "provide", "read out", "forward", "submit", "batao", "bata", "bataiye",
                       "batayein", "bataye", "bhejo", "bhejiye", "bhejiyega", "bhej do", "bhejein", "dijiye", "dijiyega", "de do", "dena", "likho", "likhiye",
                       "daalo", "dalo", "bolo", "boliye")
_SECRET_VERBS_NATIVE = ("बताइए", "बताओ", "बताएं", "बताये", "बता दो", "भेजिए", "भेजो", "भेजें", "भेज दो", "दीजिए", "दीजिये", "दे दो", "शेयर", "डालें",
                        "डालिए", "लिखिए", "बोलिए", "ಹೇಳಿ", "ಕಳುಹಿಸಿ", "ಕೊಡಿ", "ನೀಡಿ", "ಶೇರ್", "ಹಂಚಿಕೊಳ್ಳಿ", "ಹಾಕಿ", "ಬರೆಯಿರಿ")
SECRET_VERB = _alt(_SECRET_VERBS_LATIN + _SECRET_VERBS_NATIVE)
SECRET_NEG = re.compile(_alt(("never", "don't", "dont", "do not", "not", "mat", "nahi", "nahin", "kabhi", "na karein", "na kare", "na bhejein",
                              "na batayein", "नहीं", "मत", "कभी", "न करें", "न भेजें", "न बताएं", "ಬೇಡ", "ಎಂದಿಗೂ", "ಎಂದೂ", "ಇಲ್ಲ")), re.I)


def loan_rate(ctx: Ctx):
    """Reading chosen (safest): in a message or draft that talks about a loan, NO interest rate, tenure or approval/guarantee claim may
    appear (the eligibility tool returns none of those), and any rupee amount must equal max_amount_paise returned live by
    check_partner_eligibility. If the tool cannot be read, any amount is blocked."""
    lim = ctx.limits["loan"]
    for text in _texts({k: v for k, v in ctx.args.items() if k in ("body", "text")}):
        low = _fold(text)
        ctx_re = re.compile(_alt(tuple(lim["indicative_amount_context_words"]) + LOAN_CONTEXT_EXTRA), re.I)
        if not ctx_re.search(low):
            continue
        if _PCT.search(text):
            return Finding("loan message states a rate; rates are not returned by the eligibility tool")
        claim = re.compile(_alt(tuple(lim["forbidden_claim_words"]) + LOAN_CLAIM_EXTRA), re.I).search(low)
        if claim:
            return Finding(f"loan message claims {claim.group(0)!r}; only the lender decides approval")
        if _TENURE.search(text):
            return Finding("loan message states a term; terms are not returned by the eligibility tool")
        amounts = []
        for m in _RUPEES.finditer(text):
            num = (m.group("a") or m.group("b") or m.group("c")).replace(",", "")
            lakh = m.group("al") or m.group("bl")
            amounts.append(round(float(num) * (100_000 if lakh else 1) * 100))
        if amounts:
            elig = ctx.read("check_partner_eligibility", {"merchant_id": ctx.merchant_id()}) if ctx.merchant_id() else None
            allowed = elig.get("max_amount_paise") if elig and elig.get("eligible") else None
            if allowed is None or any(a != allowed for a in amounts):
                return Finding(f"loan amount in message does not match the eligibility tool result ({allowed})")
    return None


def loan_kfs(ctx: Ctx):
    """A loan application link may only go out after the approved Key Fact Statement message was sent (facts['kfs_message_sent'])."""
    if ctx.intent.action == "send_partner_application_link" and ctx.facts.get("kfs_message_sent") is not True:
        return Finding("application link requested before the Key Fact Statement message was sent")
    return None


def kyc_no_chat(ctx: Ctx):
    """Blocks the forbidden KYC/bank-change action names and any account-number-like or IFSC-like data (and sensitive-named
    fields) in args. Phone-number-like runs next to call/phone/mobile words are not account numbers."""
    if ctx.intent.action in HUMAN_ONLY_KYC_ACTIONS:
        return Finding(f"{ctx.intent.action!r} cannot be done in chat")
    for k in _keys(ctx.args):
        if any(s in k for s in SENSITIVE_KEYS):
            return Finding(f"field {k!r} (bank/KYC detail) is not accepted in chat")
    lo, hi = ctx.limits["kyc"]["account_number_min_digits"], ctx.limits["kyc"]["account_number_max_digits"]
    run = re.compile(rf"(?<!\d)\d{{{lo},{hi}}}(?!\d)")
    phone_ctx = re.compile(r"(call|phone|mobile|whatsapp|contact|helpline|number)\D{0,12}$", re.I)
    # A payment reference (UPI/UTR/RRN/txn id) is a long digit string but not an account number. Account wording always wins.
    ref_ctx = re.compile(r"(upi|utr|rrn|txn|trans\w*|ref\w*|payment\s*id)\D{0,12}$", re.I)
    acct_ctx = re.compile(r"(account|a/c|acct|khata|खाता|ಖಾತೆ)\D{0,16}$", re.I)
    for t in _texts(ctx.args):
        if re.search(r"\b[A-Za-z]{4}0[A-Za-z0-9]{6}\b", t) and re.search(r"[0-9]", t):
            return Finding("IFSC-like code found in args")
        for m in run.finditer(t):
            digits = m.group(0)
            before = t[: m.start()]
            if len(digits) == 10 and digits[0] in "6789" and phone_ctx.search(before):
                continue
            if ref_ctx.search(before) and not acct_ctx.search(before):
                continue
            return Finding("account-number-like digit string found in args")
    return None



def secret(ctx: Ctx):
    """OTP/PIN/CVV/password: blocks secret-named fields, the request_otp action, a secret word next to a 4-8 digit number (either
    order; Indic digits normalised first) and a request to share one. A warning such as 'never share your OTP' is allowed."""
    if ctx.intent.action == "request_otp":
        return Finding("requesting an OTP is never allowed")
    for k, v in _walk(ctx.args):
        if k.lower() in SECRET_KEYS and v not in (None, ""):
            return Finding(f"secret field {k!r} present in args")
    s = ctx.limits["secrets"]
    d = rf"(?<!\d)\d{{{s['min_digits']},{s['max_digits']}}}(?!\d)"
    w = SECRET_WORD
    gap = rf"[^\d]{{0,{s['proximity_chars']}}}"
    patterns = [re.compile(rf"{w}{gap}{d}", re.I), re.compile(rf"{d}{gap}{w}", re.I)]
    # Request to share: the verb may come before the secret word ("share your OTP") or after it ("apna otp bhejo", "OTP share kijiye").
    ask_before = re.compile(rf"{SECRET_VERB}[^.!?\n]{{0,30}}{w}", re.I)
    ask_after = re.compile(rf"{w}[^.!?\n]{{0,30}}{SECRET_VERB}", re.I)
    word = re.compile(w, re.I)
    for t in _texts(ctx.args, skip_ids=False):
        t = _fold(t)
        # A pure warning sentence ("Paytm never asks for your PIN or OTP.") carries a negation and no digits. It is left out of the
        # proximity scan, or the code-added compliance line would "pair" its OTP with an amount in the NEXT sentence and block every
        # first reply. A sentence with any digit, or with no negation ("my otp. 482913"), is still scanned as it stands.
        sentences = re.split(r"(?<=[.!?।\n])", t)
        scan = "".join(x for x in sentences if not (word.search(x) and SECRET_NEG.search(x) and not re.search(r"\d", x)))
        if any(p.search(scan) for p in patterns):
            return Finding("an OTP/PIN value appears in args")
        for sent in sentences:
            for pat in (ask_before, ask_after):
                m = pat.search(sent)
                # Negation is judged inside the sentence, up to a few words past the match ("otp share mat karo", "OTP kisi ko na bhejein").
                if m and not SECRET_NEG.search(sent[: m.end() + 14]):
                    return Finding("message asks the merchant to share an OTP/PIN")
    return None


def waiver_band(ctx: Ctx):
    """apply_fee_waiver months must be <= the band returned LIVE by get_offer_band (pricing system). The band is never taken from
    the intent or its facts. If the band cannot be read the waiver escalates (cannot verify). Humans: the ceiling is
    max(band, role cap) from limits.yaml; ROLE-001 blocks anything above a human's cap."""
    if ctx.intent.action != "apply_fee_waiver":
        return None
    route = ctx.limits["waiver"]["route"]
    try:
        months = int(ctx.args.get("months"))
    except (TypeError, ValueError):
        return Finding("waiver months missing or invalid", route=route)
    band = ctx.read("get_offer_band", {"merchant_id": ctx.merchant_id()})
    if band is None or "max_months" not in band:
        return Finding("offer band could not be read from the pricing system; cannot verify the waiver", route=route)
    ceiling = waiver_ceiling(ctx, int(band["max_months"]))
    if months > ceiling:
        return Finding(f"{months} months requested, band {band['max_months']} month(s) (pricing system), ceiling {ceiling}", route=route)
    return None


def _money_at_risk(ctx: Ctx) -> int:
    amounts = []
    if isinstance(ctx.args.get("amount_paise"), int):
        amounts.append(ctx.args["amount_paise"])
    sid = ctx.args.get("settlement_id") or ctx.facts.get("settlement_id")
    if sid:
        row = ctx.conn.execute("SELECT amount_paise FROM settlements WHERE settlement_id = ?", (sid,)).fetchone()
        if row:
            amounts.append(row["amount_paise"])
    return max(amounts, default=0)


def money_limit(ctx: Ctx):
    """Money at risk (args.amount_paise, or the amount of the settlement the action touches) at or above the threshold escalates
    when the actor is not already a human. The boundary is inclusive: exactly Rs 10,000 escalates."""
    if _is_human(ctx):
        return None
    lim = ctx.limits["money_limit"]
    amt = _money_at_risk(ctx)
    if amt >= lim["threshold_paise"]:
        return Finding(f"{amt} paise at risk is at or above the {lim['threshold_paise']} paise limit", route=lim["route"])
    return None


INCIDENT_HELD_ACTIONS = ("create_device_replacement", "book_visit_or_call")


def incident_hold(ctx: Ctx):
    """An area incident (#88) holds every affected merchant's per-merchant fix: a replacement cannot fix an area outage. A human may
    still act (they see the incident)."""
    if _is_human(ctx):
        return None
    mid = ctx.args.get("merchant_id")
    if not mid:
        return None
    from jobs.incidents import on_hold
    iid = on_hold(ctx.conn, mid)
    return Finding(f"{mid} is part of open area incident {iid}; per-merchant {ctx.intent.action} is held") if iid else None


def risk_hold(ctx: Ctx):
    """Any action naming a settlement that is HELD_RISK (live DB state) is for the risk reviewer only: anyone else escalates to
    that role."""
    sid = ctx.args.get("settlement_id") or ctx.facts.get("settlement_id")
    if not sid:
        return None
    row = ctx.conn.execute("SELECT status FROM settlements WHERE settlement_id = ?", (sid,)).fetchone()
    if not row or row["status"] != "HELD_RISK":
        return None
    reviewer = ctx.limits["risk_hold"]["reviewer_role"]
    p = ctx.intent.principal
    if p.kind == "HUMAN" and p.role == reviewer:
        return None
    return Finding(f"settlement {sid} is HELD_RISK; only a {reviewer} may act on it", route=ctx.limits["risk_hold"]["route"])


def contact_cap(ctx: Ctx):
    """Contact actions delegate to the injected ContactBudget (caps, quiet hours, one-cap-across-cases). With no budget injected the
    rule fails closed."""
    if not _is_contact(ctx):
        return None
    if ctx.budget is None:
        return Finding("no contact budget available; cannot verify caps and quiet hours")
    mid, ch = ctx.args.get("lead_id") or ctx.facts.get("lead_id") or ctx.merchant_id(), _channel(ctx)
    if not mid or not ch:
        return Finding("contact action without a merchant or channel")
    ok, why = ctx.budget.check(mid, ch, proactive=ctx.intent.proactive, case_id=ctx.intent.case_id)
    return None if ok else Finding(why or "contact budget refused")


def consent(ctx: Ctx):
    """A contact action needs a GRANTED consent row for that merchant (or lead) and channel. A lead (args/facts lead_id) is also
    checked against dnd_registry using the lead's public phone; merchants are not DND-checked (service messages)."""
    if not _is_contact(ctx):
        return None
    ch = _channel(ctx)
    lead = ctx.args.get("lead_id") or ctx.facts.get("lead_id")
    subject = lead or ctx.merchant_id()
    if not subject or not ch:
        return Finding("contact action without a subject or channel; consent cannot be established")
    if lead:
        row = ctx.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (lead,)).fetchone()
        phone = (row["phone_public"] if row else None) or ctx.args.get("phone")
        if phone and ctx.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (phone,)).fetchone():
            return Finding(f"lead {lead} is in the do-not-disturb registry")
    if not _consent_granted(ctx, subject, ch):
        return Finding(f"no GRANTED consent for {subject} on channel {ch!r}")
    return None


def field_notify(ctx: Ctx):
    """An internal field note goes only to a known rep or technician: never to an arbitrary id, a merchant or a lead. It is not a merchant
    contact, so consent and the contact budget do not apply; secrets and account data in its text are still caught by SECRET-001 / KYC-NO-CHAT-001."""
    pid = ctx.args.get("person_id")
    if not pid:
        return Finding("field note without a person_id")
    known = ctx.conn.execute("SELECT 1 FROM reps WHERE rep_id = ? UNION SELECT 1 FROM technicians WHERE tech_id = ?", (pid, pid)).fetchone()
    return None if known else Finding(f"{pid!r} is not a known rep or technician")


def role(ctx: Ctx):
    """A human action must be inside the actor's role (core.engine.roles matrix). A human whose waiver exceeds their role cap is
    also refused here. The same engine runs for every human; there is no bypass."""
    p = ctx.intent.principal
    if not roles_mod.may_perform(p.role, ctx.intent.action, ctx.registry):
        return Finding(f"role {p.role!r} may not perform {ctx.intent.action!r}")
    if ctx.intent.action == "apply_fee_waiver":
        band = ctx.read("get_offer_band", {"merchant_id": ctx.merchant_id()})
        try:
            months = int(ctx.args.get("months"))
        except (TypeError, ValueError):
            return None  # WAIVER-BAND-001 escalates malformed months
        if band and "max_months" in band and months > waiver_ceiling(ctx, int(band["max_months"])):
            return Finding(f"{months} months exceeds what role {p.role!r} may approve")
    return None


CLOSURE_OK = frozenset({"create_closure_request", "cancel_closure_request"})
# Once the closure EXISTS (facts['closure_created']), the follow-through is not obstruction: the pickup booking, a follow-up and the
# confirmation message. Before it exists, a retention message after a repeated request is still blocked.
CLOSURE_FOLLOW_THROUGH = frozenset({"book_visit_or_call", "schedule_follow_up", "send_message"})


def close_no_obstruct(ctx: Ctx):
    """From the Nth closure request (limits closure.obstruction_after_requests, facts['closure_requests']) any write other than
    closure handling is obstruction. Reads and drafts stay allowed. Retention messages count as obstruction by design."""
    n = ctx.facts.get("closure_requests", 0)
    if not isinstance(n, int) or n < ctx.limits["closure"]["obstruction_after_requests"]:
        return None
    if ctx.spec is not None and ctx.spec.cls not in WRITE:
        return None
    if ctx.intent.action in CLOSURE_OK:
        return None
    if ctx.facts.get("closure_created") and ctx.intent.action in CLOSURE_FOLLOW_THROUGH:
        return None
    return Finding(f"merchant has asked to close {n} times; only the closure may proceed, not {ctx.intent.action!r}")


def mandate_limit(ctx: Ctx):
    """Applies to mandate-payment actions (name contains 'mandate'; none is registered yet). CUMULATIVE: the month total already
    debited plus this payment must stay within the pre-authorised limit, and the purpose must be the allowed one."""
    if "mandate" not in ctx.intent.action:
        return None
    lim = ctx.limits["mandate"]
    f = ctx.facts
    if f.get("mandate_limit_paise") is None or f.get("mandate_month_total_paise") is None:
        return Finding("mandate limit or month total unknown; cannot verify", route=lim["route"])
    amount = ctx.args.get("amount_paise", 0)
    if f["mandate_month_total_paise"] + amount > f["mandate_limit_paise"]:
        return Finding(f"month total {f['mandate_month_total_paise']} + {amount} exceeds the limit {f['mandate_limit_paise']}", route=lim["route"])
    if f.get("mandate_purpose") != lim["allowed_purpose"]:
        return Finding(f"mandate purpose {f.get('mandate_purpose')!r} is not {lim['allowed_purpose']!r}", route=lim["route"])
    return None


def rival_target(ctx: Ctx):
    """facts['targeting'] == 'individual_rival' is blocked unless limits flags.legal_approval is on (default off)."""
    if ctx.facts.get("targeting") == "individual_rival" and not ctx.limits["flags"]["legal_approval"]:
        return Finding("targeting an individual merchant from consumer payment data needs legal approval (flag off)")
    return None


def mcp_expose(ctx: Ctx):
    """Over MCP (facts['via_mcp']) a forbidden, unregistered or not-agent-callable action is blocked."""
    if not ctx.facts.get("via_mcp"):
        return None
    a = ctx.intent.action
    if a in FORBIDDEN_ACTIONS or ctx.spec is None or not ctx.spec.agent_callable:
        return Finding(f"{a!r} is not exposed through MCP")
    return None


def consent_coach(ctx: Ctx):
    """facts['analysis'] (call or visit analysis) needs a GRANTED consent row on the analysis channel for the subject
    (args.merchant_id / facts.subject_id)."""
    if not ctx.facts.get("analysis"):
        return None
    subject = ctx.merchant_id() or ctx.facts.get("subject_id")
    ch = ctx.limits["consent"]["analysis_channel"]
    if not subject or not _consent_granted(ctx, subject, ch):
        return Finding(f"no analysis consent record for {subject!r}")
    return None


S30 = "MUNIM_Bible_Merged.md section 30"
RULES: tuple[Rule, ...] = (
    Rule("FIX-FIRST-001", "No offer action while a P0/P1 case is open", "BLOCK", f"{S30}; section 14", fix_first),
    Rule("LOAN-RATE-001", "No rate, term or approval stated unless returned by the eligibility tool", "BLOCK", f"{S30}; section 16", loan_rate,
         {"actions": ["send_message", "draft_message"]}),
    Rule("LOAN-KFS-001", "A loan application link must come with the approved Key Fact Statement message", "BLOCK", f"{S30}; section 16", loan_kfs,
         {"actions": ["send_partner_application_link"]}),
    Rule("KYC-NO-CHAT-001", "No documents, bank or KYC detail changes accepted in chat", "BLOCK", f"{S30}; section 16", kyc_no_chat),
    Rule("SECRET-001", "No PIN/OTP requested, stored or repeated", "BLOCK", f"{S30}; section 23", secret),
    Rule("WAIVER-BAND-001", "Waiver within the band returned by get_offer_band", "ESCALATE", f"{S30}; section 16", waiver_band,
         {"actions": ["apply_fee_waiver"]}),
    Rule("MONEY-LIMIT-001", "Money at risk of Rs 10,000 or more goes to a human", "ESCALATE", f"{S30}", money_limit),
    Rule("RISK-HOLD-001", "Settlement HELD_RISK is for the risk reviewer only", "ESCALATE", f"{S30}", risk_hold),
    Rule("INCIDENT-HOLD-001", "No per-merchant replacement or visit while the merchant is in an open area incident", "BLOCK", f"{S30}; section 22",
         incident_hold, {"actions": list(INCIDENT_HELD_ACTIONS)}),
    Rule("CONTACT-CAP-001", "Per-merchant contact caps and quiet hours across cases", "BLOCK", f"{S30}; section 14", contact_cap),
    Rule("CONSENT-001", "No message on a channel without consent; DND respected", "BLOCK", f"{S30}", consent),
    Rule("FIELD-NOTIFY-001", "A field note goes only to a known rep or technician", "BLOCK", f"{S30}; section 14", field_notify,
         {"actions": ["notify_field_person"]}),
    Rule("ROLE-001", "A human action must be within the actor's role", "BLOCK", f"{S30}", role, {"principals": ["HUMAN"]}),
    Rule("CLOSE-NO-OBSTRUCT-001", "After one fix offer a repeated closure request must be processed", "BLOCK", f"{S30}", close_no_obstruct),
    Rule("MANDATE-LIMIT-001", "A mandate payment must stay within the monthly limit and purpose", "ESCALATE", f"{S30}", mandate_limit),
    Rule("RIVAL-TARGET-001", "No individual-merchant targeting from consumer payment data without legal approval", "BLOCK", f"{S30}", rival_target),
    Rule("MCP-EXPOSE-001", "Forbidden actions are never exposed through MCP", "BLOCK", f"{S30}", mcp_expose),
    Rule("CONSENT-COACH-001", "No call or visit analysis without a consent record", "BLOCK", f"{S30}", consent_coach),
)


def default_rules() -> tuple[Rule, ...]:
    return RULES


def build_engine(conn, clock, registry=None, *, limits: dict | None = None, rules: Iterable[Rule] | None = None,
                 playbook_dir=None, reader=None, contact_budget=None) -> PolicyEngine:
    if registry is None:
        from packs.paytm_merchant.actions import build_registry
        registry = build_registry()
    return PolicyEngine(conn, clock, registry, rules if rules is not None else RULES, limits if limits is not None else load_limits(LIMITS_PATH),
                        playbook_dir=playbook_dir, reader=reader, contact_budget=contact_budget)
