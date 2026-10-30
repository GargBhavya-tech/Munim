"""Leads as comms recipients (lead id namespace) and internal field notes, through the REAL executor, policy engine and comms mock.

Consent and DND for a lead are checked against the leads / consent / dnd_registry tables; sending to a lead must be no easier than to a merchant.
"""
import pytest

from core.actions.types import ActionIntent, Principal
from tests.test_outreach import TS, grant, ready, w  # noqa: F401  (w is a fixture)

AGENT = Principal("AGENT", "munim")


def send(w, lead, ch="whatsapp", *, key="lead_id", step_seq=0):
    args = {key: lead, "channel": ch, "body": "Namaste, hello from Paytm. We never ask for your PIN or OTP."}
    return w.executor.submit(ActionIntent("send_message", args, step_seq=step_seq, principal=AGENT, playbook_id="OUTREACH", step="first_contact",
                                          proactive=True, channel=ch, allowlist=("send_message",), facts={"lead_id": lead}))


def outbox(w, rid):
    return w.conn.execute("SELECT * FROM comms_outbox WHERE merchant_id = ?", (rid,)).fetchall()


@pytest.mark.parametrize("key", ["lead_id", "merchant_id"])
def test_a_consented_lead_is_delivered_under_its_own_id(w, key):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    out = send(w, lid, key=key)
    assert out.status == "OK", (out.status, out.error, out.violations)
    assert [r["channel"] for r in outbox(w, lid)] == ["whatsapp"]


def test_a_lead_in_dnd_is_blocked_end_to_end_even_with_consent(w):
    lid = ready(w, "offline", dnd=True)
    grant(w, lid, "whatsapp")
    out = send(w, lid)
    assert out.status != "OK" and "CONSENT-001" in {v.rule_id for v in out.violations}
    assert outbox(w, lid) == []


def test_a_lead_with_no_consent_is_blocked_end_to_end(w):
    lid = ready(w, "offline", dnd=False)
    out = send(w, lid)
    assert out.status != "OK" and "CONSENT-001" in {v.rule_id for v in out.violations}
    assert outbox(w, lid) == []


def test_consent_on_another_channel_does_not_cover_sms(w):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    out = send(w, lid, "sms")
    assert out.status != "OK" and outbox(w, lid) == []


def test_an_unknown_lead_id_is_refused_by_the_mock(w):
    w.conn.execute("INSERT OR REPLACE INTO consent VALUES ('L-NOPE','whatsapp','GRANTED','t',?)", (TS,))
    out = send(w, "L-NOPE")
    assert out.status != "OK" and outbox(w, "L-NOPE") == []


def test_naming_both_or_neither_recipient_is_refused_before_anything_runs(w):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    for args in ({"lead_id": lid, "merchant_id": "M-0006"}, {}):
        out = w.executor.submit(ActionIntent("send_message", {**args, "channel": "whatsapp", "body": "hi"}, principal=AGENT, proactive=True,
                                             channel="whatsapp", allowlist=("send_message",)))
        assert out.status != "OK"
    assert outbox(w, lid) == []


# ---- notify_field_person ----------------------------------------------------------------------------------
def note(w, pid, body="Brief: visit 10:00."):
    return w.executor.submit(ActionIntent("notify_field_person", {"person_id": pid, "body": body, "language": "en-IN", "subject": "brief:L-1"},
                                          principal=AGENT, playbook_id="FIELD_BRIEF", step="deliver_brief", proactive=True, channel="app",
                                          allowlist=("notify_field_person",)))


def test_a_note_to_a_known_rep_is_delivered_and_a_stranger_is_not(w):
    rep = w.conn.execute("SELECT rep_id FROM reps LIMIT 1").fetchone()[0]
    ok = note(w, rep)
    assert ok.status == "OK", (ok.status, ok.error, ok.violations)
    assert [r["body"] for r in outbox(w, rep)] == ["Brief: visit 10:00."]
    bad = note(w, "M-0006")
    assert bad.status != "OK" and "FIELD-NOTIFY-001" in {v.rule_id for v in bad.violations} and outbox(w, "M-0006") == []


def test_a_note_without_the_allowlist_is_default_denied(w):
    rep = w.conn.execute("SELECT rep_id FROM reps LIMIT 1").fetchone()[0]
    out = w.executor.submit(ActionIntent("notify_field_person", {"person_id": rep, "body": "x"}, principal=AGENT, proactive=True, allowlist=("get_merchant",)))
    assert out.status != "OK" and outbox(w, rep) == []


def test_a_note_carrying_an_otp_is_still_blocked(w):
    rep = w.conn.execute("SELECT rep_id FROM reps LIMIT 1").fetchone()[0]
    out = note(w, rep, "OTP 482913")
    assert out.status != "OK" and "SECRET-001" in {v.rule_id for v in out.violations}
