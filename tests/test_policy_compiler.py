"""Plain-language policy compiler (#134). Real components: the Paytm pack rules, the real PolicyEngine, the real ledger, the frozen #139 probe set.
The optional LLM phraser is a stub (it can only propose a dict, which goes through the same validation). Native-language rule text is not
supported yet (English only), so there is nothing here for native review."""
import json
from pathlib import Path

import pytest

from core.actions.types import ActionIntent, Principal
from core.audit.ledger import Ledger
from core.judge import policy_compiler as pc
from core.judge.policy_compiler import PolicyCompileError, Probe
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.policy.rules import RULES, build_engine

FROZEN = Path(__file__).resolve().parent.parent / "eval" / "frozen" / "probes" / "policy_probes.json"
OPS = Principal("HUMAN", "u-ops", "ops_admin")
SALES = Principal("HUMAN", "u-sales", "sales_lead")


class Budget:
    def check(self, subject_id, channel, *, proactive, case_id=None):
        return (True, None)

    def record(self, *a, **k):
        pass


@pytest.fixture
def registry():
    return build_registry()


@pytest.fixture
def engine(conn, clock, http, registry):
    def read(name, args):
        r = http.get(registry.get(name).path.format(**args))
        assert r.status_code == 200, r.text
        return r.json()
    return build_engine(conn, clock, registry, reader=read, contact_budget=Budget())


@pytest.fixture
def ledger(conn, clock, engine):
    return Ledger(conn, clock, policy_hash=lambda: engine.policy_hash)


def agent(action, args, **kw):
    kw.setdefault("allowlist", (action,))
    return ActionIntent(action, args, principal=Principal("AGENT", "munim"), **kw)


def human(role, action, args):
    return ActionIntent(action, args, principal=Principal("HUMAN", f"u-{role}", role))


REPL = {"merchant_id": "M-0001"}
FROZEN_PROBES = pc.probes_from_frozen(FROZEN)


def codes(fn, *a, **kw):
    with pytest.raises(PolicyCompileError) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------------------------------------------------ text -> DSL
@pytest.mark.parametrize("text,dsl", [
    ("Block create_device_replacement for support executive", {"template": "block_action", "action": "create_device_replacement", "role": "support_executive"}),
    ("block send_message", {"template": "block_action", "action": "send_message"}),
    ("Stop field_rep from book_visit_or_call.", {"template": "block_action", "action": "book_visit_or_call", "role": "field_rep"}),
    ("Do not allow create_refund_request for sales lead", {"template": "block_action", "action": "create_refund_request", "role": "sales_lead"}),
    ("Require approval above Rs 5,000", {"template": "require_approval_above", "amount_paise": 500000, "comparison": "gt"}),
    ("require approval at or above ₹2,500.50 for create_refund_request",
     {"template": "require_approval_above", "amount_paise": 250050, "comparison": "gte", "action": "create_refund_request"}),
    ("Lower the approval limit to 7000 rupees", {"template": "require_approval_above", "amount_paise": 700000, "comparison": "gte"}),
    ("escalate over 900000 paise", {"template": "require_approval_above", "amount_paise": 900000, "comparison": "gt"}),
    ("Lower the waiver band to 1 month", {"template": "lower_waiver_band", "max_months": 1}),
])
def test_templates_parse_to_exact_dsl(text, dsl):
    assert pc.parse_text(text) == dsl


def test_money_is_parsed_not_estimated():
    assert pc.parse_money_paise("Rs 10,000") == 1_000_000
    assert pc.parse_money_paise("2 lakh") == 20_000_000
    assert pc.parse_money_paise("0.5 lakh") == 5_000_000
    assert pc.parse_money_paise("750 paise") == 750
    assert codes(pc.parse_money_paise, "12.5 paise") == "NO_TEMPLATE"


@pytest.mark.parametrize("text", [
    "block x; __import__('os').system('rm -rf /')",
    "def rule(ctx): return None",
    "make Munim nicer to merchants",
    "",
])
def test_free_form_text_or_code_matches_no_template(text):
    assert codes(pc.parse_text, text) == "NO_TEMPLATE"


# ------------------------------------------------------------------------------------------------ relaxing attempts are all rejected
@pytest.mark.parametrize("text", [
    "Allow approve_kyc for ops_admin",                               # permit a forbidden action
    "permit support_executive to apply_fee_waiver",                  # widen a role
    "let field_rep create_refund_request",                          # widen a role
    "Raise the approval limit to Rs 50,000",                        # raise a band
    "increase the waiver band to 6 months",                         # raise a band
    "remove rule KYC-NO-CHAT-001",                                  # remove a rule
    "delete the fix first rule",                                    # remove a rule
    "disable SECRET-001",                                           # remove a rule
    "stop blocking change_bank_details",                            # unblock
    "Block send_message but allow it for ops_admin",                # tightening wrapped around a relaxation
    "exempt sales_lead from the money limit",
])
def test_relaxing_text_is_rejected_before_any_parser_or_model(text):
    assert codes(pc.parse_text, text) == "RELAXING_REQUEST"


def test_a_negated_allow_is_a_block_not_a_relaxation():
    assert pc.parse_text("never allow create_refund_request")["template"] == "block_action"


@pytest.mark.parametrize("obj,code", [
    ({"template": "allow_action", "action": "approve_kyc", "role": "ops_admin"}, "INVALID_DSL"),       # no such template
    ({"template": "remove_rule", "rule_id": "KYC-NO-CHAT-001"}, "INVALID_DSL"),
    ({"template": "python", "code": "import os; os.system('x')"}, "INVALID_DSL"),                      # A24: out-of-DSL fails to load
    ({"template": "block_action", "action": "send_message", "code": "lambda ctx: None"}, "INVALID_DSL"),   # extra key
    ("lambda ctx: None", "INVALID_DSL"),
    ({"template": "block_action", "action": "send_message; import os"}, "INVALID_DSL"),
    ({"template": "block_action", "action": "approve_kyc"}, "FORBIDDEN_ACTION"),
    ({"template": "block_action", "action": "request_otp", "role": "ops_admin"}, "FORBIDDEN_ACTION"),
    ({"template": "block_action", "action": "no_such_action"}, "UNKNOWN_ACTION"),
    ({"template": "block_action", "action": "get_merchant"}, "NOT_A_WRITE"),
    ({"template": "block_action", "action": "send_message", "role": "intern"}, "UNKNOWN_ROLE"),
    ({"template": "require_approval_above", "amount_paise": 1_000_000, "comparison": "gte"}, "NOT_TIGHTENING"),   # equals today's limit
    ({"template": "require_approval_above", "amount_paise": 999_999, "comparison": "gt"}, "NOT_TIGHTENING"),     # same effective floor
    ({"template": "require_approval_above", "amount_paise": 5_000_000, "comparison": "gt"}, "NOT_TIGHTENING"),   # raises the band
    ({"template": "require_approval_above", "amount_paise": 0, "comparison": "gte"}, "INVALID_DSL"),
    ({"template": "require_approval_above", "amount_paise": 500.5, "comparison": "gte"}, "INVALID_DSL"),         # money is integer paise
    ({"template": "lower_waiver_band", "max_months": 3}, "NOT_TIGHTENING"),                                  # equals the largest cap
    ({"template": "lower_waiver_band", "max_months": 9}, "NOT_TIGHTENING"),                                  # raises the band
    ({"template": "lower_waiver_band", "max_months": -1}, "INVALID_DSL"),
])
def test_dsl_attempts_outside_the_tightening_language_are_rejected(obj, code, registry, engine):
    assert codes(pc.validate, obj, registry, engine.limits) == code


def test_trailing_junk_after_a_block_becomes_an_unknown_role_not_a_rule(engine):
    assert codes(pc.compile_text, "block send_message for everyone and also log the OTP", engine, []) == "UNKNOWN_ROLE"


def test_boundaries_just_inside_are_accepted(registry, engine):
    for obj in ({"template": "require_approval_above", "amount_paise": 999_999, "comparison": "gte"},
                {"template": "require_approval_above", "amount_paise": 999_998, "comparison": "gt"},
                {"template": "lower_waiver_band", "max_months": 2}, {"template": "lower_waiver_band", "max_months": 0}):
        pc.validate(obj, registry, engine.limits)


# ------------------------------------------------------------------------------------------------ compile -> draft -> approve -> replay refused -> revert
def test_approved_rule_refuses_a_replayed_attempt_and_revert_restores_the_baseline(engine, ledger, conn):
    attempt = agent("create_device_replacement", REPL)
    assert engine.evaluate(attempt).outcome == "ALLOW"
    baseline_hash = engine.policy_hash

    d = pc.compile_text("Block create_device_replacement", engine, FROZEN_PROBES)
    assert d.ok and d.source == "template" and d.rule_id.startswith("CUSTOM-") and len(d.rule_hash) == 64
    assert d.diff["lines"][0].startswith(f"+ {d.rule_id} [BLOCK]") and d.diff["removed"] == []
    assert engine.evaluate(attempt).outcome == "ALLOW"                      # a draft changes nothing

    r = pc.approve(engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=FROZEN_PROBES, text=d.text)
    assert r["policy_hash"] == engine.policy_hash != baseline_hash and r["policy_hash_before"] == baseline_hash
    again = engine.evaluate(attempt)                                         # the replay
    assert again.outcome == "BLOCK" and [v.rule_id for v in again.violations] == [d.rule_id]
    assert again.policy_hash == engine.policy_hash
    row = conn.execute("SELECT actor, role, payload_json FROM ledger WHERE seq = ?", (r["ledger_seq"],)).fetchone()
    p = json.loads(row["payload_json"])
    assert (row["actor"], row["role"]) == ("HUMAN:u-ops", "ops_admin") and p["op"] == pc.OP_APPROVED and p["rule_hash"] == d.rule_hash and p["dsl"] == d.dsl

    rv = pc.revert(engine, ledger, d.rule_id, OPS)
    assert engine.policy_hash == baseline_hash == rv["policy_hash"]           # byte-identical policy again
    assert engine.evaluate(attempt).outcome == "ALLOW"
    assert pc.active_compiled(conn) == []


def test_a_fresh_engine_picks_up_approved_rules_from_the_ledger(engine, ledger, conn, clock, http, registry):
    d = pc.compile_text("block create_device_replacement for support executive", engine, [])
    pc.approve(engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=[])
    fresh = build_engine(conn, clock, registry, reader=engine.reader, contact_budget=Budget())
    assert fresh.evaluate(human("support_executive", "create_device_replacement", REPL)).outcome == "ALLOW"
    pc.sync_engine(fresh, conn)
    assert fresh.evaluate(human("support_executive", "create_device_replacement", REPL)).outcome == "BLOCK"
    assert fresh.evaluate(human("technician", "create_device_replacement", REPL)).outcome == "ALLOW"       # role scoped
    assert fresh.policy_hash == engine.policy_hash
    pc.sync_engine(fresh, conn)                                                                              # idempotent
    assert [r.id for r in fresh.rules].count(d.rule_id) == 1


def test_amount_rule_boundaries_and_route(engine, ledger):
    low = agent("create_refund_request", {"merchant_id": "M-0001", "amount_paise": 500000})
    high = agent("create_refund_request", {"merchant_id": "M-0001", "amount_paise": 500001})
    assert engine.evaluate(low).outcome == engine.evaluate(high).outcome == "ALLOW"
    d = pc.compile_text("require approval above Rs 5,000", engine, [])
    pc.approve(engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=[])
    assert engine.evaluate(low).outcome == "ALLOW"                           # "above" is strict
    out = engine.evaluate(high)
    assert out.outcome == "ESCALATE" and out.violations[0].route == "ops_admin" and out.violations[0].rule_id == d.rule_id
    assert "500001 paise" in out.violations[0].reason and "500001" in out.violations[0].reason
    # humans are the approvers: the escalation does not apply to them
    assert engine.evaluate(human("ops_admin", "create_refund_request", {"merchant_id": "M-0001", "amount_paise": 900000})).outcome == "ALLOW"


def test_waiver_band_rule(engine, ledger):
    two = agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 2})
    before = engine.evaluate(two)
    d = pc.compile_text("lower the waiver band to 1 month", engine, [])
    pc.approve(engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=[])
    after = engine.evaluate(two)
    assert d.rule_id in {v.rule_id for v in after.violations}
    assert after.violations[0].route == "sales_lead" or any(v.route == "sales_lead" for v in after.violations)
    assert {"ALLOW": 0, "ESCALATE": 1, "BLOCK": 2}[after.outcome] >= {"ALLOW": 0, "ESCALATE": 1, "BLOCK": 2}[before.outcome]
    one = engine.evaluate(agent("apply_fee_waiver", {"merchant_id": "M-0001", "months": 1}))
    assert d.rule_id not in {v.rule_id for v in one.violations}


def test_policy_hash_differs_for_each_compiled_rule(engine, ledger):
    a = pc.compile_text("block create_device_replacement", engine, [])
    b = pc.compile_text("block create_refund_request", engine, [])
    pc.approve(engine, ledger, a.dsl, OPS, expected_hash=a.rule_hash, probes=[])
    h1 = engine.policy_hash
    pc.revert(engine, ledger, a.rule_id, OPS)
    pc.approve(engine, ledger, b.dsl, OPS, expected_hash=b.rule_hash, probes=[])
    assert engine.policy_hash != h1                                          # same rule source text, different parameters


# ------------------------------------------------------------------------------------------------ approval guards
def test_only_ops_admin_approves_or_reverts_and_the_exact_hash_is_required(engine, ledger, conn):
    d = pc.compile_text("block create_device_replacement", engine, [])
    n = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    for who in (SALES, Principal("AGENT", "munim"), Principal("HUMAN", "u-x", None), Principal("HUMAN", "u-r", "risk_reviewer")):
        assert codes(pc.approve, engine, ledger, d.dsl, who, expected_hash=d.rule_hash, probes=[]) == "NOT_OPS_ADMIN"
    assert codes(pc.approve, engine, ledger, d.dsl, OPS, expected_hash="0" * 64, probes=[]) == "HASH_MISMATCH"
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == n and engine.evaluate(agent("create_device_replacement", REPL)).outcome == "ALLOW"
    pc.approve(engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=[])
    assert codes(pc.revert, engine, ledger, d.rule_id, SALES) == "NOT_OPS_ADMIN"
    assert codes(pc.approve, engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=[]) == "ALREADY_ACTIVE"


def test_approve_revalidates_so_a_client_cannot_post_a_non_dsl_rule(engine, ledger):
    bad = {"template": "python", "code": "lambda ctx: None"}
    assert codes(pc.approve, engine, ledger, bad, OPS, expected_hash="x", probes=[]) == "INVALID_DSL"
    forbidden = {"template": "allow_action", "action": "approve_kyc"}
    assert codes(pc.approve, engine, ledger, forbidden, OPS, expected_hash="x", probes=[]) == "INVALID_DSL"


def test_revert_cannot_remove_a_pack_rule_and_a_tampered_ledger_cannot_load_a_relaxing_rule(engine, ledger, conn):
    assert codes(pc.revert, engine, ledger, "KYC-NO-CHAT-001", OPS) == "NOT_ACTIVE"
    assert {r.id for r in RULES} <= {r.id for r in engine.rules}
    ledger.append(kind="HUMAN_ACTION", actor="HUMAN:u-evil", role="ops_admin", payload={
        "op": pc.OP_APPROVED, "rule_id": "CUSTOM-DEADBEEF", "rule_hash": "x", "dsl": {"template": "allow_action", "action": "approve_kyc"}})
    assert codes(pc.overlay_rules, conn, engine.registry, engine.limits) == "INVALID_DSL"


# ------------------------------------------------------------------------------------------------ dry run
def test_dry_run_on_the_frozen_probes_shows_changes_and_loosens_nothing(engine):
    assert len(FROZEN_PROBES) > 100
    d = pc.compile_text("block send_message", engine, FROZEN_PROBES)
    dr = d.dry_run
    assert dr["probes_run"] == len(FROZEN_PROBES) and dr["loosened"] == [] and dr["ok"] and d.ok
    assert dr["changed"], "blocking send_message must flip some ALLOW/ESCALATE probe"
    assert all(c["before"] != "BLOCK" and c["after"] == "BLOCK" and c["action"] == "send_message" and c["new_rules"] == [d.rule_id] for c in dr["changed"])


def test_dry_run_includes_recent_ledger_attempts(engine, ledger, conn):
    ledger.append(kind="ACTION_INTENT", actor="AGENT", case_id=None, payload={"action": "create_device_replacement", "key": "k1", "cls": "R1", "args": REPL})
    probes = pc.probes_from_ledger(conn)
    assert [p.source for p in probes] == ["ledger"] and probes[0].intent.action == "create_device_replacement"
    d = pc.compile_text("block create_device_replacement", engine, probes)
    assert [(c["before"], c["after"]) for c in d.dry_run["changed"]] == [("ALLOW", "BLOCK")]


def test_dry_run_catches_a_candidate_that_drops_a_pack_rule_or_weakens_an_outcome(engine):
    assert any(p.id.startswith("PR-kyc1") for p in FROZEN_PROBES)
    cand = tuple(r for r in engine.rules if r.id != "KYC-NO-CHAT-001")
    dr = pc.dry_run(engine, cand, FROZEN_PROBES)
    assert not dr["ok"] and dr["removed_rules"] == ["KYC-NO-CHAT-001"] and dr["loosened"]
    assert any(x["lost_rules"] == ["KYC-NO-CHAT-001"] for x in dr["loosened"])


def test_a_loosening_draft_comes_back_not_ok_and_cannot_be_approved(engine, ledger, monkeypatch):
    monkeypatch.setattr(pc, "dry_run", lambda base, cand, probes: {"probes_run": 1, "changed": [], "removed_rules": [], "ok": False,
                                                                  "loosened": [{"probe": "P"}], "policy_hash_before": "a", "policy_hash_after": "b"})
    d = pc.compile_text("block send_message", engine, [])
    assert d.ok is False and d.errors == [{"code": "DRY_RUN_LOOSENED", "probe": "P"}]
    assert codes(pc.approve, engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=[]) == "DRY_RUN_LOOSENED"


def test_compiled_rules_never_make_a_forbidden_action_pass(engine, ledger):
    d = pc.compile_text("block send_message", engine, FROZEN_PROBES)
    pc.approve(engine, ledger, d.dsl, OPS, expected_hash=d.rule_hash, probes=FROZEN_PROBES)
    for p in FROZEN_PROBES:
        if p.intent.action in {"approve_kyc", "change_bank_details", "change_settlement_account", "credit_decision", "delete_record",
                               "accept_documents_in_chat", "request_otp"}:
            assert engine.evaluate(p.intent).outcome == "BLOCK", p.id


# ------------------------------------------------------------------------------------------------ optional LLM phraser (stub)
class Stub:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def propose(self, text, *, actions, roles):
        self.calls.append((text, len(actions), tuple(roles)))
        return self.reply


def test_phraser_is_not_consulted_for_a_relaxing_request(engine):
    s = Stub({"template": "block_action", "action": "send_message"})
    assert codes(pc.compile_text, "please let field reps send anything", engine, [], phraser=s) == "RELAXING_REQUEST"
    assert s.calls == []


def test_phraser_proposal_is_validated_like_any_other_dsl(engine):
    ok = Stub({"template": "block_action", "action": "send_message", "role": "field_rep"})
    d = pc.compile_text("keep field reps away from messaging customers", engine, [], phraser=ok)
    assert d.source == "llm_proposal" and d.dsl == {"template": "block_action", "action": "send_message", "role": "field_rep"} and len(ok.calls) == 1
    for reply, code in [({"template": "allow_action", "action": "approve_kyc"}, "INVALID_DSL"),
                        ({"template": "python", "code": "x"}, "INVALID_DSL"),
                        ({"template": "block_action", "action": "approve_kyc"}, "FORBIDDEN_ACTION"),
                        ({"template": "require_approval_above", "amount_paise": 9_000_000, "comparison": "gt"}, "NOT_TIGHTENING"),
                        ("def f(): pass", "INVALID_DSL")]:
        assert codes(pc.compile_text, "something vague about messaging", engine, [], phraser=Stub(reply)) == code, reply


def test_phraser_none_or_absent_gives_no_template(engine):
    assert codes(pc.compile_text, "something vague", engine, []) == "NO_TEMPLATE"
    assert codes(pc.compile_text, "something vague", engine, [], phraser=Stub(None)) == "NO_TEMPLATE"


def test_template_match_never_calls_the_phraser(engine):
    s = Stub({"template": "block_action", "action": "send_message"})
    d = pc.compile_text("block create_refund_request", engine, [], phraser=s)
    assert s.calls == [] and d.dsl["action"] == "create_refund_request"


def test_the_rule_definition_that_feeds_policy_hash_carries_the_dsl_hash(registry, engine):
    dsl = pc.validate({"template": "require_approval_above", "amount_paise": 500000, "comparison": "gt"}, registry, engine.limits)
    rule = pc.build_rule(dsl)
    assert pc.rule_hash(dsl) in rule.definition()["source"] and rule.id == "CUSTOM-" + pc.rule_hash(dsl)[:8].upper()
    other = pc.build_rule(pc.validate({"template": "require_approval_above", "amount_paise": 500000, "comparison": "gte"}, registry, engine.limits))
    assert other.definition()["source"] != rule.definition()["source"] and other.id != rule.id
