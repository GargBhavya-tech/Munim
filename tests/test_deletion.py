import pytest

from core.audit.ledger import Ledger, verify_chain
from core.engine.contact_budget import ContactBudget
from core.memory.crm import CRMGateway
from core.memory.deletion import LEGALLY_REQUIRED, REDACTED, delete_merchant_memory, withdraw_consent
from core.memory.episodes import EpisodeStore
from core.memory.profile import ConsentWithdrawn, ProfileStore
from core.memory.timeline import Timeline
from tests.test_timeline import mk_case, mk_event

M, OTHER = "M-0001", "M-0002"


@pytest.fixture
def w(conn, clock):
    mk_case(conn, "C-1", M, "2026-10-05T09:00:00+05:30")
    mk_case(conn, "C-2", OTHER, "2026-10-05T09:00:00+05:30")
    conn.execute("UPDATE cases SET resume_note='merchant said shop closes at 7' WHERE case_id='C-1'")
    mk_event(conn, "EV-1", M, "C-1", "KYC_REJECTED", "2026-10-05T09:01:00+05:30")
    led = Ledger(conn, clock)
    for i in range(5):
        led.append(kind="ACTION_RESULT", actor="ENGINE", case_id="C-1", payload={"action": "send_kyc_link", "status": "OK", "i": i})
    ps = ProfileStore(conn, clock)
    for mid in (M, OTHER):
        ps.set_fact(mid, "language", "hi", "MERCHANT_SAID")
        ps.set_fact(mid, "kyc_status", "PENDING", "SYSTEM")
    CRMGateway(conn, clock).agent_write(M, "stage", "ACTIVATING")
    CRMGateway(conn, clock).agent_write(OTHER, "stage", "NEW")
    for cid in ("C-1", "C-2"):
        conn.execute("INSERT INTO turns(case_id,ts_sim,role,text_redacted,decision_json) VALUES (?,?,?,?,?)", (cid, "t", "merchant", f"secret words {cid}", '{"x":1}'))
    EpisodeStore(conn, clock).summarise_case("C-1")
    return conn, led, ps


def test_delete_removes_memory_and_ledger_chain_still_ok(w, clock):
    conn, led, ps = w
    before = conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
    assert verify_chain(conn).ok and conn.execute("SELECT COUNT(*) FROM episodes WHERE merchant_id=?", (M,)).fetchone()[0] == 1
    counts = delete_merchant_memory(conn, clock, M, ledger=led)
    assert counts["profile_facts"] == 2 and counts["episodes"] == 1 and counts["crm_fields"] == 1 and counts["turns_redacted"] == 1
    assert ps.history(M) == [] and ps.current(M) == {}
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE merchant_id=?", (M,)).fetchone()[0] == 0
    assert conn.execute("SELECT text_redacted, decision_json FROM turns WHERE case_id='C-1'").fetchone()[:] == (REDACTED, None)
    assert conn.execute("SELECT resume_note FROM cases WHERE case_id='C-1'").fetchone()[0] is None
    assert CRMGateway(conn, clock).history(M, "stage") == []
    r = verify_chain(conn)
    assert r.ok and r.entries == len(before) + 1  # old rows untouched, one deletion record appended
    assert [tuple(x) for x in conn.execute("SELECT * FROM ledger ORDER BY seq LIMIT ?", (len(before),))] == [tuple(x) for x in before]


def test_other_merchants_untouched(w, clock):
    conn, led, ps = w
    delete_merchant_memory(conn, clock, M, ledger=led)
    assert ps.get(OTHER, "language").value == "hi"
    assert conn.execute("SELECT text_redacted FROM turns WHERE case_id='C-2'").fetchone()[0] == "secret words C-2"
    assert CRMGateway(conn, clock).history(OTHER, "stage")


def test_legally_required_records_kept(w, clock):
    conn, led, _ = w
    conn.execute("INSERT OR REPLACE INTO consent(subject_id,channel,status,source,ts) VALUES (?,?,?,?,?)", (M, "sms", "REVOKED", "stop", "t"))
    conn.execute("INSERT INTO contact_touches(subject_id,channel,case_id,direction,ts) VALUES (?,?,?,?,?)", (M, "sms", "C-1", "OUT", "t"))
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_payments = conn.execute("SELECT COUNT(*) FROM payments WHERE merchant_id=?", (M,)).fetchone()[0]
    delete_merchant_memory(conn, clock, M, ledger=led)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n_events
    assert conn.execute("SELECT COUNT(*) FROM payments WHERE merchant_id=?", (M,)).fetchone()[0] == n_payments
    assert conn.execute("SELECT COUNT(*) FROM contact_touches WHERE subject_id=?", (M,)).fetchone()[0] == 1
    assert conn.execute("SELECT status FROM consent WHERE subject_id=? AND channel='sms'", (M,)).fetchone()[0] == "REVOKED"
    assert conn.execute("SELECT COUNT(*) FROM cases WHERE merchant_id=?", (M,)).fetchone()[0] == 1
    assert {"ledger", "events", "consent", "contact_touches", "payments"} <= set(LEGALLY_REQUIRED)


def test_naive_ledger_delete_is_blocked_and_breaks_chain_if_forced(w, tmp_path, db_path):
    """A naive implementation (DELETE FROM ledger WHERE case belongs to merchant) is stopped by the trigger,
    and when the trigger is removed on a copy the chain check fails -- the reason deletion must not touch the ledger."""
    conn, _, _ = w
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM ledger WHERE case_id='C-1'")
    assert verify_chain(conn).ok
    conn.execute("VACUUM INTO ?", (str(tmp_path / "copy.db"),))
    from core.memory.store import connect
    c = connect(tmp_path / "copy.db")
    c.execute("DROP TRIGGER ledger_no_delete")
    c.execute("DELETE FROM ledger WHERE case_id='C-1' AND seq=3")
    r = verify_chain(c)
    assert not r.ok and r.first_mismatch == 3


def test_deletion_stops_memory_use_and_rebuild(w, clock):
    conn, led, ps = w
    delete_merchant_memory(conn, clock, M, ledger=led)
    with pytest.raises(ConsentWithdrawn):
        ps.set_fact(M, "language", "en", "MERCHANT_SAID")
    assert Timeline(conn, clock).build(M)["cases"] == []
    assert EpisodeStore(conn, clock).summarise_case("C-1")["stored"] is False


def test_withdrawn_scope_stops_use_without_deleting(w, clock):
    conn, _, ps = w
    ps.set_fact(M, "prefers_whatsapp", "yes", "MERCHANT_SAID", consent_scope="whatsapp")
    withdraw_consent(conn, clock, M, "whatsapp")
    assert "prefers_whatsapp" not in ps.current(M) and "language" in ps.current(M)
    assert len(ps.history(M, "prefers_whatsapp")) == 1  # not deleted


def test_stop_message_via_contact_budget_hides_channel_scoped_facts(w, clock):
    conn, _, ps = w
    ps.set_fact(M, "prefers_whatsapp", "yes", "MERCHANT_SAID", consent_scope="whatsapp")
    ContactBudget(conn, clock).record_stop(M)
    assert "prefers_whatsapp" not in ps.current(M) and "language" in ps.current(M)
