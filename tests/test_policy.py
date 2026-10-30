"""Policy engine (#29): default deny, hash, precedence, quiet hours, and a hand-written outcome table."""
from dataclasses import replace
from datetime import timedelta

import pytest

from core.actions.types import ActionIntent, Principal
from core.engine.policy import DEFAULT_DENY_ID, Finding, PolicyEngine, Rule, compute_policy_hash, load_limits
from packs.paytm_merchant.actions import SPECS, build_registry
from packs.paytm_merchant.policy.rules import LIMITS_PATH, RULES, build_engine


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
def reader(http, registry):
    return make_reader(http, registry)


@pytest.fixture
def engine(conn, clock, registry, reader):
    return build_engine(conn, clock, registry, reader=reader, contact_budget=FakeBudget())


def ids(decision):
    return {v.rule_id for v in decision.violations}


# ---- default deny ------------------------------------------------------------------------------------
def test_unknown_write_action_is_denied(engine):
    d = engine.evaluate(agent("brand_new_action", {"merchant_id": "M-0001"}))
    assert d.outcome == "BLOCK" and ids(d) == {DEFAULT_DENY_ID}


def test_default_deny_when_no_rule_matches_a_new_action(conn, clock, registry):
    """An engine with ZERO rules still denies writes that are not registered / not on the step allowlist."""
    bare = PolicyEngine(conn, clock, registry, [], load_limits(LIMITS_PATH))
    assert bare.evaluate(agent("brand_new_action", {})).outcome == "BLOCK"
    assert bare.evaluate(agent("create_device_replacement", {"merchant_id": "M-0001"}, allowlist=("send_message",))).outcome == "BLOCK"
    assert bare.evaluate(agent("create_device_replacement", {"merchant_id": "M-0001"}, allowlist=None)).outcome == "BLOCK"
    assert bare.evaluate(agent("create_device_replacement", {"merchant_id": "M-0001"})).outcome == "ALLOW"
    assert bare.evaluate(agent("get_merchant", {"merchant_id": "M-0001"}, allowlist=None)).outcome == "ALLOW"   # R0 always allowed
    assert bare.evaluate(agent("draft_message", {"merchant_id": "M-0001", "text": "hi"}, allowlist=None)).outcome == "ALLOW"


def test_human_skips_allowlist_but_not_rules_and_there_is_no_admin_bypass(engine):
    ok = engine.evaluate(human("support_executive", "create_device_replacement", {"merchant_id": "M-0001"}, allowlist=None))
    assert ok.outcome == "ALLOW"
    # ops_admin is the strongest role and is still stopped by the content rules and by the role reservation
    d = engine.evaluate(human("ops_admin", "send_message", {"merchant_id": "M-0001", "channel": "whatsapp", "body": "your OTP 123456"}, channel="whatsapp"))
    assert d.outcome == "BLOCK" and "SECRET-001" in ids(d)
    assert "ROLE-001" in ids(engine.evaluate(human("ops_admin", "clear_risk_hold", {"settlement_id": "S-0060"})))


def test_a_rule_that_crashes_denies(conn, clock, registry):
    def boom(ctx):
        raise RuntimeError("x")
    eng = PolicyEngine(conn, clock, registry, [Rule("BOOM-001", "t", "WARN", "s", boom)], load_limits(LIMITS_PATH))
    d = eng.evaluate(agent("get_merchant", {"merchant_id": "M-0001"}))
    assert d.outcome == "BLOCK" and ids(d) == {"BOOM-001"}


# ---- hash -------------------------------------------------------------------------------------------
def test_every_decision_carries_the_hash(engine):
    d = engine.evaluate(agent("get_merchant", {"merchant_id": "M-0001"}))
    assert d.policy_hash == engine.policy_hash and len(d.policy_hash) == 64
    d2 = engine.evaluate(agent("brand_new", {}))
    assert all(v.policy_hash == engine.policy_hash for v in d2.violations)


def _hash(rules=RULES, limits=None, pb=None):
    return compute_policy_hash(rules, limits if limits is not None else load_limits(LIMITS_PATH), pb)


def test_hash_is_stable_and_changes_with_any_rule_limit_or_playbook(tmp_path):
    base = _hash()
    assert base == _hash()
    for field, val in (("severity", "WARN"), ("title", "changed"), ("source", "elsewhere")):
        changed = (replace(RULES[0], **{field: val}),) + RULES[1:]
        assert _hash(changed) != base, field
    assert _hash(RULES[1:]) != base                                                   # a rule removed
    other_cond = (replace(RULES[0], condition=lambda ctx: Finding("different logic")),) + RULES[1:]
    assert _hash(other_cond) != base                                                  # the condition changed
    lim = load_limits(LIMITS_PATH)
    lim["money_limit"]["threshold_paise"] += 1
    assert _hash(limits=lim) != base
    (tmp_path / "C01.yaml").write_text("steps: [a]")
    with_pb = _hash(pb=tmp_path)
    assert with_pb != base
    (tmp_path / "C01.yaml").write_text("steps: [a, b]")
    assert _hash(pb=tmp_path) != with_pb


# ---- precedence and order -------------------------------------------------------------------------------
def test_block_beats_escalate_and_all_violations_are_returned_in_stable_order(engine):
    # 6-month waiver (ESCALATE) while a P1 is open (BLOCK)
    it = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6}, open_case_lanes=("P1",))
    d = engine.evaluate(it)
    assert d.outcome == "BLOCK"
    assert [(v.rule_id, v.severity) for v in d.violations] == [("FIX-FIRST-001", "BLOCK"), ("WAIVER-BAND-001", "ESCALATE")]
    assert engine.evaluate(it) == d


def test_warn_never_changes_the_outcome(conn, clock, registry):
    eng = PolicyEngine(conn, clock, registry, [Rule("W-1", "t", "WARN", "s", lambda ctx: Finding("fyi"))], load_limits(LIMITS_PATH))
    d = eng.evaluate(agent("get_merchant", {"merchant_id": "M-0001"}))
    assert d.outcome == "ALLOW" and ids(d) == {"W-1"} and d.allowed


def test_escalate_names_the_human_role(engine):
    d = engine.evaluate(agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6}))
    assert d.outcome == "ESCALATE" and d.violations[0].route == "sales_lead"


# ---- quiet hours via CONTACT-CAP-001 ---------------------------------------------------------------------
def test_quiet_hours_r3_is_blocked_through_the_contact_budget(conn, clock, registry, reader):
    budget = FakeBudget(ok=False, why="quiet hours 21:00-08:00")
    eng = build_engine(conn, clock, registry, reader=reader, contact_budget=budget)
    clock.advance(timedelta(hours=13))                                  # 22:00 sim
    d = eng.evaluate(agent("send_message", {"merchant_id": "M-0001", "channel": "whatsapp", "body": "hello"}, channel="whatsapp", proactive=True, case_id="C-1"))
    assert d.outcome == "BLOCK" and ids(d) == {"CONTACT-CAP-001"}
    assert "quiet hours" in d.violations[0].reason
    assert budget.calls == [("M-0001", "whatsapp", True, "C-1")]


def test_contact_without_a_budget_fails_closed(conn, clock, registry, reader):
    eng = build_engine(conn, clock, registry, reader=reader)
    d = eng.evaluate(agent("send_message", {"merchant_id": "M-0001", "channel": "whatsapp", "body": "hello"}, channel="whatsapp"))
    assert "CONTACT-CAP-001" in ids(d)


# ---- hand-written outcome table (expected values are NOT computed by the engine) ---------------------------------
WA = {"merchant_id": "M-0001", "channel": "whatsapp", "body": "Your device is fixed."}
TABLE = [
    # id, intent, outcome, exact rule ids
    ("read-open", agent("get_offer_band", {"merchant_id": "M-0001"}, allowlist=None), "ALLOW", set()),
    ("draft-open", agent("draft_message", {"merchant_id": "M-0001", "text": "hello"}, allowlist=None), "ALLOW", set()),
    ("waiver-1-band-1", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}), "ALLOW", set()),
    ("waiver-6-band-1", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 6}), "ESCALATE", {"WAIVER-BAND-001"}),
    ("waiver-2-band-1", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 2}), "ESCALATE", {"WAIVER-BAND-001"}),
    ("waiver-2-d2c-band-2", agent("apply_fee_waiver", {"merchant_id": "M-0009", "months": 2}), "ALLOW", set()),
    ("waiver-3-d2c-band-2", agent("apply_fee_waiver", {"merchant_id": "M-0009", "months": 3}), "ESCALATE", {"WAIVER-BAND-001"}),
    ("waiver-during-p1", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}, open_case_lanes=("P1",)), "BLOCK", {"FIX-FIRST-001"}),
    ("waiver-during-p2", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}, open_case_lanes=("P2",)), "ALLOW", set()),
    ("order-during-p0", agent("create_device_order", {"merchant_id": "M-0001"}, open_case_lanes=("P0", "P3")), "BLOCK", {"FIX-FIRST-001"}),
    ("order-clear", agent("create_device_order", {"merchant_id": "M-0001"}), "ALLOW", set()),
    ("replacement-during-p1-is-service-not-offer", agent("create_device_replacement", {"merchant_id": "M-0001"}, open_case_lanes=("P1",)), "ALLOW", set()),
    ("not-on-allowlist", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}, allowlist=("send_message",)), "BLOCK", {DEFAULT_DENY_ID}),
    ("write-without-allowlist", agent("create_device_replacement", {"merchant_id": "M-0001"}, allowlist=None), "BLOCK", {DEFAULT_DENY_ID}),
    ("unknown-action", agent("delete_everything", {}), "BLOCK", {DEFAULT_DENY_ID}),
    ("forbidden-approve-kyc", agent("approve_kyc", {"merchant_id": "M-0001"}), "BLOCK", {DEFAULT_DENY_ID, "KYC-NO-CHAT-001"}),
    ("forbidden-request-otp", agent("request_otp", {"merchant_id": "M-0001"}), "BLOCK", {DEFAULT_DENY_ID, "SECRET-001"}),
    ("admin-change-bank", human("ops_admin", "change_bank_details", {"merchant_id": "M-0001"}), "BLOCK", {DEFAULT_DENY_ID, "KYC-NO-CHAT-001", "ROLE-001"}),
    ("agent-clears-hold", agent("clear_risk_hold", {"settlement_id": "S-0060"}), "BLOCK", {DEFAULT_DENY_ID, "RISK-HOLD-001", "MONEY-LIMIT-001"}),
    ("risk-reviewer-clears-hold", human("risk_reviewer", "clear_risk_hold", {"settlement_id": "S-0060"}), "ALLOW", set()),
    ("support-clears-hold", human("support_executive", "clear_risk_hold", {"settlement_id": "S-0060"}), "BLOCK", {"ROLE-001", "RISK-HOLD-001"}),
    ("sales-clears-hold", human("sales_lead", "clear_risk_hold", {"settlement_id": "S-0060"}), "BLOCK", {"ROLE-001", "RISK-HOLD-001"}),
    ("ops-clears-hold", human("ops_admin", "clear_risk_hold", {"settlement_id": "S-0060"}), "BLOCK", {"ROLE-001", "RISK-HOLD-001"}),
    ("auditor-clears-hold", human("auditor", "clear_risk_hold", {"settlement_id": "S-0060"}), "BLOCK", {"ROLE-001", "RISK-HOLD-001"}),
    ("auditor-reads", human("auditor", "get_merchant", {"merchant_id": "M-0001"}), "ALLOW", set()),
    ("auditor-writes", human("auditor", "schedule_follow_up", {"merchant_id": "M-0001", "due_at": "2026-11-01T10:00:00+05:30"}), "BLOCK", {"ROLE-001"}),
    ("human-without-role", ActionIntent("schedule_follow_up", {"merchant_id": "M-0001", "due_at": "x"}, principal=Principal("HUMAN", "u", None)), "BLOCK", {"ROLE-001"}),
    ("support-waiver-in-band", human("support_executive", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}), "ALLOW", set()),
    ("support-waiver-out-of-band", human("support_executive", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 2}), "BLOCK", {"ROLE-001", "WAIVER-BAND-001"}),
    ("sales-waiver-3-at-cap", human("sales_lead", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 3}), "ALLOW", set()),
    ("sales-waiver-4-over-cap", human("sales_lead", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 4}), "BLOCK", {"ROLE-001", "WAIVER-BAND-001"}),
    ("area-waiver-2", human("area_manager", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 2}), "ALLOW", set()),
    ("area-waiver-3", human("area_manager", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 3}), "BLOCK", {"ROLE-001", "WAIVER-BAND-001"}),
    ("field-rep-waiver", human("field_rep", "apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}), "BLOCK", {"ROLE-001"}),
    ("technician-ticket", human("technician", "raise_ticket", {"merchant_id": "M-0001", "type": "DEVICE"}), "ALLOW", set()),
    ("refund-just-under-limit", agent("create_refund_request", {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 999_999}), "ALLOW", set()),
    ("refund-at-limit", agent("create_refund_request", {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 1_000_000}), "ESCALATE", {"MONEY-LIMIT-001"}),
    ("refund-human-big", human("support_executive", "create_refund_request", {"merchant_id": "M-0001", "payment_id": "P-1", "amount_paise": 5_000_000}), "ALLOW", set()),
    ("message-ok", agent("send_message", WA, channel="whatsapp"), "ALLOW", set()),
    ("message-channel-from-args", agent("send_message", WA), "ALLOW", set()),
    ("message-no-sms-consent", agent("send_message", {**WA, "channel": "sms"}, channel="sms"), "BLOCK", {"CONSENT-001"}),
    ("message-with-otp", agent("send_message", {**WA, "body": "Your OTP is 482913"}, channel="whatsapp"), "BLOCK", {"SECRET-001"}),
    ("message-warns-about-otp", agent("send_message", {**WA, "body": "Never share your OTP with anyone."}, channel="whatsapp"), "ALLOW", set()),
    ("message-with-account", agent("send_message", {**WA, "body": "Send it to account 123456789012"}, channel="whatsapp"), "BLOCK", {"KYC-NO-CHAT-001"}),
    ("link-without-kfs", agent("send_partner_application_link", {"merchant_id": "M-0001"}, channel="whatsapp"), "BLOCK", {"LOAN-KFS-001"}),
    ("link-with-kfs", agent("send_partner_application_link", {"merchant_id": "M-0001"}, channel="whatsapp", facts={"kfs_message_sent": True}), "ALLOW", set()),
    ("closure-second-request-blocks-offer", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}, facts={"closure_requests": 2}), "BLOCK", {"CLOSE-NO-OBSTRUCT-001"}),
    ("closure-second-request-processes", agent("create_closure_request", {"merchant_id": "M-0001"}, facts={"closure_requests": 2}), "ALLOW", set()),
    ("closure-first-request-may-offer", agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}, facts={"closure_requests": 1}), "ALLOW", set()),
    ("mcp-read", agent("get_merchant", {"merchant_id": "M-0001"}, facts={"via_mcp": True}), "ALLOW", set()),
    ("mcp-forbidden", agent("approve_kyc", {"merchant_id": "M-0001"}, facts={"via_mcp": True}), "BLOCK", {DEFAULT_DENY_ID, "KYC-NO-CHAT-001", "MCP-EXPOSE-001"}),
    ("rival-targeting", agent("get_merchant", {"merchant_id": "M-0001"}, facts={"targeting": "individual_rival"}), "BLOCK", {"RIVAL-TARGET-001"}),
    ("analysis-without-consent", agent("get_merchant", {"merchant_id": "M-0001"}, facts={"analysis": True}), "BLOCK", {"CONSENT-COACH-001"}),
]


@pytest.mark.parametrize("intent,outcome,rule_ids", [pytest.param(i, o, r, id=n) for n, i, o, r in TABLE])
def test_outcome_table(engine, intent, outcome, rule_ids):
    d = engine.evaluate(intent)
    assert (d.outcome, ids(d)) == (outcome, rule_ids), [(v.rule_id, v.reason) for v in d.violations]


def test_table_has_enough_rows():
    assert len(TABLE) >= 40
