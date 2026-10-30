"""Case facts the playbooks' escalation rules need that are about THIS case's history, not just live state."""
from __future__ import annotations

from datetime import timedelta

from core.actions.types import Reader
from core.engine.clock import SimClock, iso, parse_iso


def replacements_before_case(reader: Reader, clock: SimClock, case, window_days: int = 30) -> int:
    """Soundbox replacements in the last 30 days that were created BEFORE this case opened.

    A replacement this case created is in flight, not a repeat. Counting it made S1 escalate to a human on the next
    unrelated event while its own replacement was still on the way. Cancelled replacements never count.
    """
    if not case.merchant_id:
        return 0
    cutoff = iso(clock.now() - timedelta(days=window_days))
    reps = reader("get_device_status", {"merchant_id": case.merchant_id}).get("replacements", [])
    opened = parse_iso(case.opened_at)
    return sum(1 for r in reps if r["status"] != "CANCELLED" and r["created_at"] >= cutoff and parse_iso(r["created_at"]) < opened)


KFS_PHRASE = "key fact statement"


def conversational_steps(playbook) -> list[str]:
    """Steps that only a conversation can complete: their done-expression refers to their own ``steps.<name>.done`` (C2's checklist)."""
    return [st.name for st in playbook.steps if f"steps.{st.name}.done" in (st.done or "")]


def conversational_steps_done(conn, case, names) -> list[str]:
    """A conversational step is done once Munim sent the case a message during that step AND the merchant answered after it
    (they went through the checklist; the next step's live reads say whether it worked). Never True for any other step:
    a reply must not complete a step whose done is a system state (the "closing on KYC submitted" mistake)."""
    out = []
    for name in names:
        sent = conn.execute("SELECT MIN(e.seq) FROM engine_intents i JOIN events e ON e.event_id = i.event_id "
                            "WHERE i.case_id = ? AND i.step = ? AND i.kind = 'message' AND i.status = 'DONE'", (case.case_id, name)).fetchone()[0]
        if sent is None:
            continue
        reply = conn.execute("SELECT 1 FROM case_events ce JOIN events e ON e.event_id = ce.event_id WHERE ce.case_id = ? AND e.seq > ? "
                             "AND e.type IN ('MERCHANT_MESSAGE', 'LEAD_REPLY') LIMIT 1", (case.case_id, sent)).fetchone()
        if reply:
            out.append(name)
    return out


def make_facts_fn(conn, reader: Reader, playbooks=None):
    """The standard ``facts_fn`` for the Engine: facts the policy rules and playbooks need that only the case's own history knows.

    intent             the intent carried by the triggering event
    closure_requests   how many times the merchant asked to close in this case (CLOSE-NO-OBSTRUCT-001)
    closure_created    a closure request now exists in the system, so the follow-through may proceed
    kfs_message_sent   the approved Key Fact Statement line has already gone out in this case (LOAN-KFS-001)
    steps.<n>.done     a conversational step (C2's checklist) was sent and answered; needs ``playbooks``
    follow_up_pending  a follow-up timer is scheduled and has not fired (a promised re-check keeps the case open)
    Derived from the audit trail and a live read; never from anything a model said.
    """
    import json

    def facts(case, ev) -> dict:
        out: dict = {"intent": ev.payload.get("intent")}
        rows = conn.execute("SELECT e.payload_json FROM case_events ce JOIN events e ON e.event_id = ce.event_id "
                            "WHERE ce.case_id = ? AND e.type = 'MERCHANT_MESSAGE'", (case.case_id,)).fetchall()
        out["closure_requests"] = sum(1 for r in rows if json.loads(r["payload_json"]).get("intent") == "CLOSURE_REQUEST")
        if out["closure_requests"] or case.playbook_id.startswith("C7"):
            closures = reader("get_closures", {"merchant_id": case.merchant_id}).get("closures", []) if case.merchant_id else []
            out["closure_created"] = any(c["status"] != "CANCELLED" for c in closures)
        sent = conn.execute("SELECT payload_json FROM ledger WHERE kind = 'MESSAGE_SENT' AND case_id = ?", (case.case_id,)).fetchall()
        out["kfs_message_sent"] = any(KFS_PHRASE in (json.loads(r["payload_json"]).get("text") or "").lower() for r in sent)
        out["follow_up_pending"] = conn.execute("SELECT 1 FROM timers WHERE case_id = ? AND kind = 'follow_up' AND status = 'PENDING'",
                                                (case.case_id,)).fetchone() is not None
        pb = (playbooks or {}).get(case.playbook_id)
        if pb is not None:
            done = conversational_steps_done(conn, case, conversational_steps(pb))
            if done:
                out["steps"] = {name: {"done": True} for name in done}
        return out

    return facts
