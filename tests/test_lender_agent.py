"""#115 lender agent: a signed request through the executor; the lender decides; a lender that does not answer never leads to a decision by Munim.

The lender's agent is a mock route (GET /mock/lender/agent, the signed request in the query: R0 reads are GET). Munim's client reaches it only through core.actions.executor (``ask_lender_agent``), so the
ledger holds exactly what was sent, written before the request.
"""
from datetime import timedelta
from pathlib import Path

import pytest

from core.engine.clock import iso
from integrations import lender_agent as LA
from integrations.lender_agent import SCOPE, LenderClient, merchant_token, sign
from packs.paytm_merchant.mocks.lender import SIMULATED_SIGNING_KEY as KEY, eligibility
from tests.world import World


def pick(conn, clock, want: bool):
    for r in conn.execute("SELECT merchant_id FROM merchants ORDER BY merchant_id"):
        if eligibility(conn, r[0], clock.now())["eligible"] == want:
            return r[0]
    raise AssertionError(f"no seeded merchant with eligible={want}")


def consent(conn, clock, mid, status="GRANTED"):
    conn.execute("INSERT OR REPLACE INTO consent(subject_id, channel, status, source, ts) VALUES (?,?,?,?,?)", (mid, SCOPE, status, "test", iso(clock.now())))


@pytest.fixture
def w(db_path, clock):
    return World(db_path, clock, None)


@pytest.fixture
def client(w):
    return LenderClient(w.conn, w.clock, w.ledger, w.executor, KEY)


def sent_requests(w):
    """What left the system: the ledger's ACTION_INTENT args for the lender request (written before the call)."""
    return [e.payload["args"] for e in w.ledger.entries() if e.kind == "ACTION_INTENT" and e.payload.get("action") == LA.ACTION]


def lender_down(w, on=True):
    w.http.post("/mock/chaos", json={"system": "lender", "mode": "error500", "rate": 1.0 if on else 0})


def body(clock, mid="M-0001", **kw):
    return {"merchant_token": merchant_token(KEY, mid), "consent_scope": SCOPE, "purpose": "eligibility_check", "ts": iso(clock.now()), **kw}


def test_the_lenders_own_decision_is_relayed_unchanged_for_eligible_and_ineligible(w, client):
    for want in (True, False):
        mid = pick(w.conn, w.clock, want)
        consent(w.conn, w.clock, mid)
        a = client.check(mid)
        lender = eligibility(w.conn, mid, w.clock.now())
        assert a.status == "DECIDED" and a.eligible is want and a.max_amount_paise == lender["max_amount_paise"] and a.reasons == tuple(lender["reasons"])
        assert (a.kfs_ref is not None) == want and (not want or a.kfs_ref.startswith("KFS-"))


def test_the_request_carries_an_opaque_token_never_the_merchant_id_and_is_ledgered_first(w, client):
    mid = pick(w.conn, w.clock, True)
    consent(w.conn, w.clock, mid)
    client.check(mid, case_id="CS-L1")
    [sent] = sent_requests(w)
    assert set(sent) == {"merchant_token", "consent_scope", "purpose", "ts", "signature"} and mid not in str(sent)
    kinds = [e.kind for e in w.ledger.entries() if e.case_id == "CS-L1"]
    assert kinds.index("ACTION_INTENT") < kinds.index("ACTION_RESULT")                   # intent before the request, result after


def test_the_model_can_never_call_the_lender_itself(w):
    assert LA.ACTION not in w.registry.names(agent_only=True)


def test_a_lender_that_does_not_answer_gives_a_holding_answer_and_no_decision(w, client):
    mid = pick(w.conn, w.clock, True)
    consent(w.conn, w.clock, mid)
    lender_down(w)
    a = client.check(mid, language="en-IN", case_id="CS-0001")
    assert a.status == "PENDING" and a.say == "I'll check with the lender and come back to you."
    assert a.eligible is None and a.max_amount_paise is None and a.kfs_ref is None and a.reasons == ()      # nothing to quote, nothing Munim decided
    assert [e.payload["lender_agent"] for e in w.ledger.entries() if "lender_agent" in e.payload] == ["PENDING"]
    lender_down(w, False)
    w.clock.advance(timedelta(minutes=10))                                                  # past the circuit breaker's cool-down
    assert client.check(mid).status == "DECIDED"                                            # when the lender answers, its answer is what counts


def test_the_holding_answer_is_in_the_merchants_language(w, client):
    mid = pick(w.conn, w.clock, False)
    consent(w.conn, w.clock, mid)
    lender_down(w)
    assert client.check(mid, language="hinglish").say == LA.SAY_PENDING["hinglish"]
    assert client.check(mid, language="xx").say == LA.SAY_PENDING["en-IN"]


@pytest.mark.parametrize("status", ["REVOKED", "DENIED"])
def test_without_a_granted_consent_nothing_is_sent(w, client, status):
    mid = pick(w.conn, w.clock, True)
    assert client.check(mid).status == "NO_CONSENT"                                          # no consent row at all
    consent(w.conn, w.clock, mid, status)
    assert client.check(mid).status == "NO_CONSENT" and sent_requests(w) == []


def test_the_lender_refuses_a_bad_signature_a_wrong_scope_a_stale_request_and_an_unknown_token(w):
    def ask(b, sig):
        return w.http.get("/mock/lender/agent", params={**b, "signature": sig})
    good = body(w.clock)
    tampered = ask({**good, "merchant_token": merchant_token(KEY, "M-0002")}, sign(KEY, good))
    assert tampered.status_code == 401 and "BAD_SIGNATURE" in tampered.text                 # the body was changed after signing
    wrong = body(w.clock, consent_scope="marketing")
    assert "SCOPE_NOT_ACCEPTED" in ask(wrong, sign(KEY, wrong)).text
    old = body(w.clock)
    w.clock.advance(timedelta(minutes=6))
    assert "STALE_REQUEST" in ask(old, sign(KEY, old)).text
    ghost = body(w.clock, merchant_token="mt_" + "0" * 20)
    assert "UNKNOWN_TOKEN" in ask(ghost, sign(KEY, ghost)).text
    assert "BAD_SIGNATURE" in ask(body(w.clock), sign(b"someone-elses-key", body(w.clock))).text


def test_a_lender_refusal_reaches_the_client_as_refused_not_as_a_decision(w):
    mid = pick(w.conn, w.clock, True)
    consent(w.conn, w.clock, mid)
    a = LenderClient(w.conn, w.clock, w.ledger, w.executor, b"wrong").check(mid)              # Munim signs with the wrong key
    assert a.status == "REFUSED" and a.eligible is None and "BAD_SIGNATURE" in a.detail


def test_munim_never_computes_eligibility_itself_and_never_touches_a_mock():
    src = Path(LA.__file__).read_text(encoding="utf-8")
    assert "eligibility(" not in src[src.index("class LenderClient"):]
    code = src.split('"""', 2)[2]                                                            # everything after the module docstring
    assert "mocks" not in code and "/mock/" not in code and "TestClient" not in code
