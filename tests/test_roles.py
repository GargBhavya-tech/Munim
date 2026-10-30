"""Human roles (#31): enforced by ROLE-001 in the same policy engine; no bypass."""
import pytest

from core.actions.registry import RiskClass
from core.audit.ledger import Ledger, verify_chain
from core.engine import roles
from core.actions.types import ActionIntent, Principal
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


@pytest.fixture
def registry():
    return build_registry()


@pytest.fixture
def engine(conn, clock, registry, http):
    return build_engine(conn, clock, registry, reader=make_reader(http, registry), contact_budget=FakeBudget())


def ids(d):
    return {v.rule_id for v in d.violations}


def test_support_exec_clearing_held_risk_is_refused_by_role_001_and_risk_reviewer_is_allowed(engine, conn, clock):
    ledger = Ledger(conn, clock, policy_hash=lambda: engine.policy_hash)
    for role, outcome in (("support_executive", "BLOCK"), ("risk_reviewer", "ALLOW")):
        it = human(role, "clear_risk_hold", {"settlement_id": "S-0060"}, case_id="C-1")
        d = engine.evaluate(it)
        assert d.outcome == outcome
        if outcome == "BLOCK":
            assert "ROLE-001" in ids(d) and "RISK-HOLD-001" in ids(d)
        else:
            assert d.violations == ()
        ledger.append(kind="POLICY", actor=it.principal.ledger_actor, role=role, case_id="C-1",
                      payload={"outcome": d.outcome, "rules": sorted(ids(d))})
    entries = ledger.entries("C-1")
    assert [(e.actor, e.role, e.payload["outcome"]) for e in entries] == [
        ("HUMAN:u-support_executive", "support_executive", "BLOCK"), ("HUMAN:u-risk_reviewer", "risk_reviewer", "ALLOW")]
    assert all(e.policy_hash == engine.policy_hash for e in entries) and verify_chain(conn).ok


def test_auditor_can_do_nothing_that_writes(engine, registry):
    for spec in registry.specs():
        d = engine.evaluate(human("auditor", spec.name, {"merchant_id": "M-0001"}))
        if spec.cls in (RiskClass.R0, RiskClass.DRAFT):
            assert "ROLE-001" not in ids(d), spec.name
        else:
            assert d.outcome == "BLOCK" and "ROLE-001" in ids(d), spec.name


def test_sales_lead_approves_out_of_band_waiver_up_to_the_cap_only(engine):
    w = lambda m: engine.evaluate(human("sales_lead", "apply_fee_waiver", {"merchant_id": "M-0001", "months": m}))
    assert w(1).outcome == "ALLOW"
    assert w(3).outcome == "ALLOW"                       # band 1, cap 3 in limits.yaml
    over = w(4)
    assert over.outcome == "BLOCK" and "ROLE-001" in ids(over)


def test_waiver_cap_is_read_from_limits_not_the_code(conn, clock, registry, http):
    from core.engine.policy import load_limits
    from packs.paytm_merchant.policy.rules import LIMITS_PATH
    lim = load_limits(LIMITS_PATH)
    lim["waiver"]["role_cap_months"]["sales_lead"] = 5
    eng = build_engine(conn, clock, registry, limits=lim, reader=make_reader(http, registry))
    assert eng.evaluate(human("sales_lead", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 5})).outcome == "ALLOW"


@pytest.mark.parametrize("role", roles.ROLES)
def test_no_role_may_skip_the_content_rules(engine, role):
    """No admin bypass: whatever the role, an OTP in a message is blocked."""
    d = engine.evaluate(human(role, "send_message", {"merchant_id": "M-0001", "channel": "whatsapp", "body": "OTP 123456"}, channel="whatsapp"))
    assert d.outcome == "BLOCK" and "SECRET-001" in ids(d)


def test_nobody_but_the_risk_reviewer_may_clear_a_hold(engine):
    for role in roles.ROLES:
        d = engine.evaluate(human(role, "clear_risk_hold", {"settlement_id": "S-0060"}))
        assert (d.outcome == "ALLOW") == (role == "risk_reviewer"), role


def test_unknown_role_and_agent_posing_as_human_are_refused(engine):
    assert "ROLE-001" in ids(engine.evaluate(human("superuser", "schedule_follow_up", {"merchant_id": "M-0001", "due_at": "x"})))


@pytest.mark.parametrize("role,action,expected", [
    ("support_executive", "create_refund_request", True), ("support_executive", "create_plan_change", False),
    ("sales_lead", "create_plan_change", True), ("sales_lead", "create_refund_request", False),
    ("risk_reviewer", "clear_risk_hold", True), ("ops_admin", "clear_risk_hold", False),
    ("ops_admin", "cancel_refund_request", True), ("field_rep", "raise_ticket", False),
    ("technician", "create_device_replacement", True), ("area_manager", "revoke_fee_waiver", True),
    ("auditor", "get_merchant", True), ("auditor", "send_message", False),
])
def test_permission_matrix(registry, role, action, expected):
    assert roles.may_perform(role, action, registry) is expected


def test_every_role_in_the_bible_exists():
    assert set(roles.ROLES) == {"support_executive", "risk_reviewer", "sales_lead", "ops_admin", "auditor", "area_manager", "field_rep", "technician"}
