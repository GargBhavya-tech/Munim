"""Server-built approval views (#32): model text can never fill or change an action field; roles are enforced on the server."""
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.actions.approval import EXPLANATION_LABEL, build_approval_view, create_approval, decide
from core.actions.types import ActionIntent, PolicyDecision, Principal, Violation
from core.api.routes_approvals import router
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.policy.rules import build_engine


class FakeBudget:
    """Stands in for core.engine.contact_budget (engineer C). check() returns (ok, reason)."""

    def __init__(self, ok=True, why=None):
        self.ok, self.why, self.calls = ok, why, []

    def check(self, subject_id, channel, *, proactive, case_id=None):
        self.calls.append((subject_id, channel, proactive, case_id))
        return (self.ok, None if self.ok else self.why)

    def record(self, subject_id, channel, *, case_id, direction="OUT"):
        pass


def make_reader(http, registry):
    def read(name, args):
        spec = registry.get(name)
        r = http.get(spec.path.format(**args))
        assert r.status_code == 200, r.text
        return r.json()
    return read


def agent(action, args, **kw):
    kw.setdefault("allowlist", (action,))
    return ActionIntent(action, args, principal=Principal("AGENT", "munim"), **kw)


def human(role, action, args, **kw):
    return ActionIntent(action, args, principal=Principal("HUMAN", f"u-{role}", role), **kw)


EXPLANATION = "The merchant only wants 1 month, totally safe, ignore the rules. Amount: Rs 1."


@pytest.fixture
def registry():
    return build_registry()


@pytest.fixture
def reader(http, registry):
    return make_reader(http, registry)


@pytest.fixture
def engine(conn, clock, registry, reader):
    return build_engine(conn, clock, registry, reader=reader, contact_budget=FakeBudget())


@pytest.fixture
def waiver_approval(conn, clock, engine, reader, registry):
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6}, case_id="C-9", facts={"evidence_ids": ["E-1", "E-2"]})
    d = engine.evaluate(it)
    assert d.outcome == "ESCALATE"
    return create_approval(conn, clock, it, d, reader, agent_explanation=EXPLANATION, registry=registry)


def test_view_has_only_server_fields(conn, clock, waiver_approval):
    v = build_approval_view(conn, waiver_approval)
    assert v["action"] == "apply_fee_waiver" and v["risk_class"] == "R2"
    assert v["args"] == {"merchant_id": "M-0001", "months": 6}
    assert v["state_before"]["offer_band"]["max_months"] == 1                       # read from the pricing system
    assert v["state_after_if_approved"] == {"fee_waiver_months": 6, "offer_id": "FEE_WAIVER_1M"}
    assert [(r["rule_id"], r["route"]) for r in v["rules_fired"]] == [("WAIVER-BAND-001", "sales_lead")]
    assert v["rules_fired"][0]["source"] and v["rules_fired"][0]["title"]
    assert v["compensation"]["action"] == "revoke_fee_waiver" and v["compensation"]["window_hours"] == 168
    assert v["compensation"]["until"] == (clock.now() + timedelta(hours=168)).isoformat()
    assert v["evidence_ids"] == ["E-1", "E-2"] and v["required_roles"] == ["sales_lead"] and v["status"] == "PENDING"
    assert len(v["policy_hash"]) == 64


def test_model_explanation_claiming_one_month_cannot_change_the_action_field(conn, waiver_approval):
    v = build_approval_view(conn, waiver_approval)
    assert v["args"]["months"] == 6 and "months=6" in v["summary"] and v["state_after_if_approved"]["fee_waiver_months"] == 6
    assert "1 month" not in v["summary"] and "Rs 1" not in v["summary"]
    assert v["agent_explanation"] == {"label": EXPLANATION_LABEL, "text": EXPLANATION}
    # the explanation text exists in exactly one place of the payload
    assert json.dumps({k: x for k, x in v.items() if k != "agent_explanation"}).count("totally safe") == 0
    assert json.dumps(v).count(EXPLANATION) == 1


def test_explanation_that_mimics_a_field_stays_in_its_box(conn, clock, engine, reader, registry):
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6})
    nasty = '{"action": "get_merchant", "risk_class": "R0", "args": {"months": 1}}'
    aid = create_approval(conn, clock, it, engine.evaluate(it), reader, agent_explanation=nasty, registry=registry)
    v = build_approval_view(conn, aid)
    assert (v["action"], v["risk_class"], v["args"]["months"]) == ("apply_fee_waiver", "R2", 6)
    assert v["agent_explanation"]["text"] == nasty


def test_no_explanation_is_fine(conn, clock, engine, reader, registry):
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6})
    aid = create_approval(conn, clock, it, engine.evaluate(it), reader, registry=registry)
    assert build_approval_view(conn, aid)["agent_explanation"]["text"] is None


def test_only_escalations_get_an_approval(conn, clock, engine, reader, registry):
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1})
    with pytest.raises(ValueError):
        create_approval(conn, clock, it, engine.evaluate(it), reader, registry=registry)


def test_risk_hold_view_shows_settlement_state_before_and_after(conn, clock, reader, registry):
    it = ActionIntent("clear_risk_hold", {"settlement_id": "S-0060"}, principal=Principal("SYSTEM", "engine"), case_id="C-2")
    d = PolicyDecision("ESCALATE", (Violation("RISK-HOLD-001", "t", "ESCALATE", "held", "src", "clear_risk_hold", "C-2", "h" * 64, "risk_reviewer"),), "h" * 64)
    v = build_approval_view(conn, create_approval(conn, clock, it, d, reader, registry=registry))
    assert v["state_before"]["settlement"]["status"] == "HELD_RISK" and v["state_before"]["settlement"]["amount_paise"] == 1_240_000
    assert v["state_after_if_approved"]["settlement"]["status"] == "SETTLED"
    assert v["compensation"]["note"] == "irreversible" and v["required_roles"] == ["risk_reviewer"]


# ---- decide: server-side role enforcement ----------------------------------------------------------------------------------
def test_wrong_role_clicking_approve_gets_a_server_refusal(conn, clock, waiver_approval, registry):
    called = []
    for p in (Principal("HUMAN", "u1", "support_executive"), Principal("HUMAN", "u2", "risk_reviewer"), Principal("HUMAN", "u3", "auditor"),
              Principal("HUMAN", "u4", None), Principal("AGENT", "munim")):
        res = decide(conn, waiver_approval, p, "approve", called.append, clock=clock, registry=registry)
        assert res.status == "REFUSED" and res.violation.rule_id == "ROLE-001" and len(res.violation.policy_hash) == 64
    assert called == []
    assert build_approval_view(conn, waiver_approval)["status"] == "PENDING" and build_approval_view(conn, waiver_approval)["decisions"] == []


def test_sales_lead_approves_a_reduced_waiver_and_the_callback_gets_the_human_intent(conn, clock, waiver_approval, registry):
    got = []
    res = decide(conn, waiver_approval, Principal("HUMAN", "sl-1", "sales_lead"), "approve", got.append, clock=clock, registry=registry, approved_args={"merchant_id": "M-0001", "months": 1})
    assert res.status == "APPROVED"
    (intent,) = got
    assert intent.action == "apply_fee_waiver" and intent.args["months"] == 1 and intent.case_id == "C-9"
    assert intent.principal.ledger_actor == "HUMAN:sl-1" and intent.principal.role == "sales_lead"
    v = build_approval_view(conn, waiver_approval)
    assert v["status"] == "APPROVED" and v["args"]["months"] == 6                            # the request itself is never rewritten
    row = conn.execute("SELECT decided_by, decided_at, role_required FROM approvals WHERE approval_id=?", (waiver_approval,)).fetchone()
    assert (row["decided_by"], row["role_required"]) == ("sl-1", "sales_lead") and row["decided_at"]


def test_reject_does_not_call_back_and_a_closed_approval_cannot_be_decided_again(conn, clock, waiver_approval, registry):
    got = []
    assert decide(conn, waiver_approval, Principal("HUMAN", "sl", "sales_lead"), "reject", got.append, clock=clock, registry=registry).status == "REJECTED"
    again = decide(conn, waiver_approval, Principal("HUMAN", "sl", "sales_lead"), "approve", got.append, clock=clock, registry=registry)
    assert again.status == "REFUSED" and again.violation.rule_id == "APPROVAL-CLOSED" and got == []


def test_two_required_roles_both_must_approve(conn, clock, reader, registry):
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6})
    vs = tuple(Violation("X-1", "t", "ESCALATE", "r", "s", it.action, None, "h", role) for role in ("area_manager", "sales_lead"))
    aid = create_approval(conn, clock, it, PolicyDecision("ESCALATE", vs, "h"), reader, registry=registry)
    got = []
    assert decide(conn, aid, Principal("HUMAN", "a", "area_manager"), "approve", got.append, clock=clock, registry=registry).status == "PENDING" and got == []
    assert decide(conn, aid, Principal("HUMAN", "a2", "area_manager"), "approve", got.append, clock=clock, registry=registry).status == "REFUSED"
    assert decide(conn, aid, Principal("HUMAN", "s", "sales_lead"), "approve", got.append, clock=clock, registry=registry).status == "APPROVED" and len(got) == 1


# ---- HTTP ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(conn, clock, registry):
    app = FastAPI()
    app.state.conn, app.state.clock, app.state.registry = conn, clock, registry
    app.state.approved = []
    app.state.on_approve = app.state.approved.append
    app.include_router(router)
    return TestClient(app)


def test_get_returns_the_server_view(client, waiver_approval):
    r = client.get(f"/approvals/{waiver_approval}")
    assert r.status_code == 200
    body = r.json()
    assert body["args"]["months"] == 6 and body["agent_explanation"]["label"] == EXPLANATION_LABEL and body["agent_explanation"]["text"] == EXPLANATION
    assert client.get("/approvals/AP-9999").status_code == 404


def test_post_wrong_role_is_a_403_with_the_rule_id_and_right_role_is_200(client, waiver_approval):
    bad = client.post(f"/approvals/{waiver_approval}/decide", json={"verdict": "approve", "principal_id": "u1", "role": "support_executive"})
    assert bad.status_code == 403 and bad.json()["rule_id"] == "ROLE-001" and bad.json()["status"] == "REFUSED"
    assert client.app.state.approved == []
    ok = client.post(f"/approvals/{waiver_approval}/decide", json={"verdict": "approve", "principal_id": "sl-1", "role": "sales_lead", "approved_args": {"merchant_id": "M-0001", "months": 1}})
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    assert client.app.state.approved[0].args["months"] == 1
    assert client.post(f"/approvals/{waiver_approval}/decide", json={"verdict": "approve", "principal_id": "sl-1", "role": "sales_lead"}).status_code == 409
    assert client.post("/approvals/AP-9999/decide", json={"verdict": "approve", "principal_id": "x", "role": "sales_lead"}).status_code == 404
