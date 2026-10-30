"""#102 consented first contact: consent + DND hold, identity/AI/PIN-OTP lines present, no cold links, contact goes through the executor and policy.

The REAL policy engine, contact budget, ledger and executor are used. Most tests use a recording stand-in transport (``StubHttp``); the last test uses the real comms mock.
"""
from datetime import timedelta

import pytest

from core.actions.executor import Executor
from core.engine.scripted import scripted_decider
from core.gate.phrases import Phrases, canon
from jobs.dedupe import DedupeJob, outreach_ready
from jobs.lead_lifecycle import LeadLifecycle
from jobs.outreach import (BLOCKED, FIELD_ROUTED, NO_CHANNEL, NOT_ELIGIBLE, SEND_FAILED, SENT, FirstContactInvalid, OutreachJob, build_first_contact,
                           check_first_contact, contains_link, segment_for)
from tests.test_scenarios_s1_c2 import SCRIPT
from tests.world import World

PH = Phrases()
TS = "2026-10-31T09:00:00+05:30"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._b, self.text = status_code, body, str(body)

    def json(self):
        return self._b


class StubHttp:
    """Records every request; answers a comms send like the mock would for a known recipient."""

    def __init__(self, status=201):
        self.calls, self.status = [], status

    def request(self, method, path, json=None, headers=None):
        self.calls.append((method, path, json, (headers or {}).get("Idempotency-Key")))
        return _Resp(self.status, {"comm_id": f"C-{len(self.calls)}", "status": "SENT"} if self.status < 300 else {"error": "BOOM"})


@pytest.fixture
def w(db_path, clock):
    w = World(db_path, clock, None, mid="M-0006")
    w.engine.decider = scripted_decider(SCRIPT)
    DedupeJob(w.conn, clock, w.ledger).run()
    return w


@pytest.fixture
def stub():
    return StubHttp()


@pytest.fixture
def job(w, stub):
    ex = Executor(w.conn, w.clock, w.ledger, w.registry, stub, w.policy)
    life = LeadLifecycle(w.conn, w.clock, w.ledger, w.engine, w.timers)
    return OutreachJob(w.conn, w.clock, w.ledger, ex, life, w.budget)


def grant(w, lead_id, *channels):
    for ch in channels:
        w.conn.execute("INSERT OR REPLACE INTO consent VALUES (?,?,?,?,?)", (lead_id, ch, "GRANTED", "opt_in", TS))


def ready(w, category=None, dnd=None):
    for r in outreach_ready(w.conn):
        in_dnd = w.conn.execute("SELECT 1 FROM dnd_registry WHERE phone = ?", (r["phone_public"],)).fetchone() is not None
        if (category is None or segment_for(r["category"]) == category) and (dnd is None or in_dnd == dnd):
            return r["lead_id"]
    raise AssertionError("no such lead in the seed")


def state(w, lid):
    return w.conn.execute("SELECT state FROM leads WHERE lead_id = ?", (lid,)).fetchone()[0]


# ---------------------------------------------------------------------------------------------------------- the message
@pytest.mark.parametrize("lang", ["en-IN", "hi-IN", "hinglish", "kn-IN"])
def test_first_contact_carries_identity_ai_disclosure_and_pin_otp_in_every_language(lang):
    body = build_first_contact(lang, PH)
    assert canon(PH.text("identity", lang)) in canon(body) and canon(PH.text("pin_otp", lang)) in canon(body)
    assert body.index(PH.text("identity", lang)) < body.index(PH.text("pin_otp", lang))
    assert check_first_contact(body, lang, PH) == []


def test_english_first_contact_text_is_exactly_the_approved_lines():
    assert build_first_contact("en-IN", PH) == "Hello. I am Munim, Paytm's AI assistant. Paytm never asks for your PIN or OTP."


def test_a_body_without_the_identity_or_pin_line_or_with_a_link_is_refused():
    assert check_first_contact("Hello. Paytm never asks for your PIN or OTP.", "en-IN", PH) == ["missing:identity"]
    assert check_first_contact("Hello. I am Munim, Paytm's AI assistant.", "en-IN", PH) == ["missing:pin_otp"]
    ok = build_first_contact("en-IN", PH)
    for bad in ("https://paytm.example/start", "pay at www.paytm.example", "download paytm.me/x", "get it from bit.ly/abc", "install app.apk"):
        assert check_first_contact(f"{ok} {bad}", "en-IN", PH) == ["link_in_cold_message"], bad
    assert not contains_link(ok)


def test_the_composer_refuses_to_build_a_first_contact_when_a_required_phrase_is_missing(monkeypatch):
    broken = Phrases()
    monkeypatch.setattr("jobs.outreach.REQUIRED_PHRASES", ("identity", "pin_otp", "ai_yes"))       # a required line the body does not carry
    with pytest.raises(FirstContactInvalid):
        build_first_contact("en-IN", broken)


# ---------------------------------------------------------------------------------------------------------- happy path via the executor
def test_a_consented_offline_lead_gets_one_whatsapp_through_the_executor(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    res = job.first_contact(lid)
    assert (res.status, res.channel) == (SENT, "whatsapp")
    [(method, path, body, key)] = stub.calls
    assert (method, path) == ("POST", "/mock/comms/send") and key
    assert body == {"merchant_id": lid, "channel": "whatsapp", "body": build_first_contact("en-IN", PH), "language": "en-IN"}
    assert state(w, lid) == "CONTACTED"
    assert [dict(r)["direction"] for r in w.conn.execute("SELECT direction FROM contact_touches WHERE subject_id = ?", (lid,))] == ["OUT"]
    kinds = [r["kind"] for r in w.conn.execute("SELECT kind FROM ledger WHERE kind IN ('ACTION_INTENT', 'POLICY', 'ACTION_RESULT') ORDER BY seq")]
    assert kinds == ["ACTION_INTENT", "POLICY", "ACTION_RESULT"] and len(stub.calls) == 1                              # ledger before the side effect's result


def test_the_send_is_a_proactive_contact_governed_by_the_contact_cap(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    w.clock.advance(timedelta(hours=13))                                                             # 22:00, inside quiet hours
    res = job.first_contact(lid)
    assert res.status == BLOCKED and "CONTACT-CAP-001" in res.rule_ids and stub.calls == []
    assert state(w, lid) == "NEW"


def test_a_rerun_after_a_crash_replays_the_same_key_and_sends_once(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    assert job.first_contact(lid).status == SENT
    # the process died after the send but before the bookkeeping: put the lead back and forget the touch
    w.conn.execute("UPDATE leads SET state = 'RESEARCHED' WHERE lead_id = ?", (lid,))
    w.conn.execute("DELETE FROM contact_touches WHERE subject_id = ?", (lid,))
    again = job.first_contact(lid)
    assert again.status == SENT and len(stub.calls) == 1                                             # replayed, not re-sent
    assert w.conn.execute("SELECT COUNT(*) FROM contact_touches WHERE subject_id = ?", (lid,)).fetchone()[0] == 0   # a replay is not counted twice


# ---------------------------------------------------------------------------------------------------------- Watch out: DND "because it's B2B"
def test_a_dnd_number_is_never_contacted_even_with_consent_and_even_as_a_business(w, job, stub):
    lid = ready(w, "offline", dnd=True)
    grant(w, lid, "whatsapp", "voice", "email")                                                      # consent is on file: DND still wins
    res = job.first_contact(lid)
    assert res.status == BLOCKED and res.rule_ids == ["DND"] and stub.calls == []
    assert state(w, lid) == "NEW"


def test_the_policy_engine_is_a_second_wall_when_the_job_check_is_missing(w, job, stub, monkeypatch):
    lid = ready(w, "offline", dnd=True)
    grant(w, lid, "whatsapp")
    monkeypatch.setattr(OutreachJob, "in_dnd", lambda self, row: False)                              # mutation: the job's own DND check removed
    res = job.first_contact(lid)
    assert res.status == BLOCKED and "CONSENT-001" in res.rule_ids and stub.calls == []


def test_a_d2c_lead_in_dnd_is_blocked_on_email_too(w, job, stub):
    row = w.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (ready(w, "offline", dnd=True),)).fetchone()
    w.conn.execute("UPDATE leads SET category = 'd2c' WHERE lead_id = ?", (row["lead_id"],))
    grant(w, row["lead_id"], "email")
    assert job.first_contact(row["lead_id"]).status == BLOCKED and stub.calls == []


# ---------------------------------------------------------------------------------------------------------- consent and channel by segment
def test_no_consent_means_no_contact(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    res = job.first_contact(lid)
    assert res.status == NO_CHANNEL and stub.calls == [] and state(w, lid) == "NEW"


def test_a_revoked_consent_is_not_a_consent(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    w.conn.execute("UPDATE consent SET status = 'REVOKED' WHERE subject_id = ?", (lid,))
    assert job.first_contact(lid).status == NO_CHANNEL and stub.calls == []


def test_channel_is_chosen_by_segment_from_consented_channels_only(w, job, stub):
    off = ready(w, "offline", dnd=False)
    grant(w, off, "email", "sms", "voice")                                                           # email and sms are not first-contact channels for a shop
    assert job.choose_channel(w.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (off,)).fetchone()) == "voice"
    grant(w, off, "whatsapp")
    assert job.choose_channel(w.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (off,)).fetchone()) == "whatsapp"   # WhatsApp before a call
    w.conn.execute("UPDATE leads SET category = 'd2c' WHERE lead_id = ?", (off,))
    assert job.choose_channel(w.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (off,)).fetchone()) == "email"       # D2C: email
    w.conn.execute("DELETE FROM consent WHERE subject_id = ? AND channel = 'email'", (off,))
    assert job.choose_channel(w.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (off,)).fetchone()) is None          # never a guess


def test_a_d2c_lead_gets_an_email_with_the_same_lines(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    w.conn.execute("UPDATE leads SET category = 'd2c' WHERE lead_id = ?", (lid,))
    grant(w, lid, "email")
    res = job.first_contact(lid, "en-IN")
    assert (res.status, res.channel) == (SENT, "email") and stub.calls[0][2]["channel"] == "email"
    assert check_first_contact(stub.calls[0][2]["body"], "en-IN", PH) == []


def test_a_high_value_offline_lead_is_routed_to_a_person_and_nothing_is_sent(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "field_visit", "whatsapp")
    w.conn.execute("UPDATE leads SET score = 9 WHERE lead_id = ?", (lid,))
    res = job.first_contact(lid)
    assert res.status == FIELD_ROUTED and stub.calls == [] and state(w, lid) == "CONTACTED"


def test_a_low_value_lead_is_not_routed_to_a_field_visit(w, job, stub):
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "field_visit")
    w.conn.execute("UPDATE leads SET score = 2 WHERE lead_id = ?", (lid,))
    assert job.first_contact(lid).status == NO_CHANNEL


# ---------------------------------------------------------------------------------------------------------- only dedupe-cleared leads
def test_only_the_outreach_ready_set_is_ever_contacted(w, job, stub):
    held = w.conn.execute("SELECT lead_id, state FROM leads WHERE state IN ('EXISTING_MERCHANT', 'NEEDS_REVIEW', 'DUPLICATE') ORDER BY lead_id").fetchall()
    assert {"EXISTING_MERCHANT", "NEEDS_REVIEW"} <= {r["state"] for r in held}
    for r in held:
        grant(w, r["lead_id"], "whatsapp", "email")
        assert job.first_contact(r["lead_id"]).status == NOT_ELIGIBLE, r["lead_id"]
    assert stub.calls == []


def test_a_lead_never_matched_against_merchants_is_not_contacted(w, job, stub):
    fresh = w.conn.execute("SELECT lead_id FROM leads WHERE state = 'NEW' AND provenance_json NOT LIKE '%CLEAR%' LIMIT 1").fetchone()
    if fresh is None:                                                                                # dedupe already ran in the fixture: create the case
        lid = ready(w, "offline", dnd=False)
        w.conn.execute("UPDATE leads SET provenance_json = '{}' WHERE lead_id = ?", (lid,))
    else:
        lid = fresh[0]
    grant(w, lid, "whatsapp")
    assert job.first_contact(lid).status == NOT_ELIGIBLE and stub.calls == []


def test_run_contacts_only_ready_consented_non_dnd_leads(w, job, stub):
    ids = [r["lead_id"] for r in outreach_ready(w.conn)][:6]
    for lid in ids:
        grant(w, lid, "whatsapp")
    results = {r.lead_id: r.status for r in job.run(limit=len(outreach_ready(w.conn)))}
    sent = {lid for lid, s in results.items() if s == SENT}
    assert sent and sent <= set(ids)
    dnd = {r["phone"] for r in w.conn.execute("SELECT phone FROM dnd_registry")}
    assert not {w.conn.execute("SELECT phone_public FROM leads WHERE lead_id = ?", (l,)).fetchone()[0] for l in sent} & dnd
    assert len(stub.calls) == len(sent)


# ---------------------------------------------------------------------------------------------------------- failure honesty
def test_a_failed_send_changes_no_state_and_counts_no_touch(w, db_path, clock):
    ex = Executor(w.conn, clock, w.ledger, w.registry, StubHttp(status=422), w.policy)
    life = LeadLifecycle(w.conn, clock, w.ledger, w.engine, w.timers)
    j = OutreachJob(w.conn, clock, w.ledger, ex, life, w.budget)
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    res = j.first_contact(lid)
    assert res.status == SEND_FAILED and state(w, lid) == "NEW"
    assert w.conn.execute("SELECT COUNT(*) FROM contact_touches WHERE subject_id = ?", (lid,)).fetchone()[0] == 0


def test_against_the_real_comms_mock_a_consented_lead_receives_the_message(w, clock):
    """The comms mock accepts a lead recipient (lead id namespace): the real transport lands the message in the outbox under the lead id."""
    life = LeadLifecycle(w.conn, clock, w.ledger, w.engine, w.timers)
    j = OutreachJob(w.conn, clock, w.ledger, w.executor, life, w.budget)
    lid = ready(w, "offline", dnd=False)
    grant(w, lid, "whatsapp")
    res = j.first_contact(lid)
    assert res.status == SENT
    rows = w.conn.execute("SELECT merchant_id, channel, body FROM comms_outbox WHERE merchant_id = ?", (lid,)).fetchall()
    assert len(rows) == 1 and rows[0]["channel"] == "whatsapp" and rows[0]["body"] == res.body
    assert state(w, lid) == "CONTACTED"
