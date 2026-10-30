"""GATE 2: forbidden never executes. The spine can act safely without any model in the loop.

500 randomised proposed actions run through the REAL pipeline (executor -> real policy engine -> real mocks):
forbidden names, out-of-band arguments, wrong roles, offers during open service cases, sends in quiet hours,
sends without consent, secrets, rate promises and more.

Every expectation below is HAND-WRITTEN from the Bible (section 30 rules table, section 14, section 16). None of it is computed
by calling the policy engine: a test that derived "expected" from the policy function would agree with its own bugs.

Pass condition: 0 forbidden or out-of-policy actions reach a mock, every proposal ends in exactly the expected outcome,
and every refusal or escalation names its rule id.
"""
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.actions.executor import Executor
from core.actions.registry import FORBIDDEN_ACTIONS
from core.actions.types import AGENT, ActionIntent, Principal
from core.audit.ledger import Ledger, verify_chain
from core.engine.clock import IST, SimClock
from core.engine.contact_budget import ContactBudget
from core.memory.store import connect
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.mocks import create_app
from packs.paytm_merchant.policy.rules import build_engine

PB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "playbooks"
N = 500
REG = build_registry()
ALLOW = tuple(REG.names(agent_only=True))                 # a broad allowlist, so the RULES (not the allowlist) are what is under test
DAY = (2026, 10, 31)

SUPPORT = Principal("HUMAN", "h-support", "support_executive")
SALES = Principal("HUMAN", "h-sales", "sales_lead")
RISK = Principal("HUMAN", "h-risk", "risk_reviewer")
OPS = Principal("HUMAN", "h-ops", "ops_admin")
AUDITOR = Principal("HUMAN", "h-audit", "auditor")
TECH = Principal("HUMAN", "h-tech", "technician")


@dataclass
class P:
    """One proposed action and what MUST happen to it."""
    tid: str
    intent: ActionIntent
    expect: str                      # OK (reaches a mock) | REFUSED | ESCALATE | NOT_OK (anything except OK)
    rules: frozenset = frozenset()   # rule ids that must be named
    at: tuple = (10, 0)              # hour, minute on the sim day
    route: str | None = None


def mk(i, action, args, principal=AGENT, *, lanes=(), allow=ALLOW, facts=None, channel=None, proactive=False, lane="P1"):
    return ActionIntent(action, args, case_id="CS-GATE", step_seq=i, principal=principal, playbook_id="GATE", step="gate", proactive=proactive,
                        lane=lane, open_case_lanes=tuple(lanes), channel=channel, allowlist=None if principal.kind == "HUMAN" else tuple(allow),
                        facts=facts or {})


def S(*rules):
    return frozenset(rules)


# ---- the hand-written table: (id, builder(rng, i, env) -> P) -------------------------------------------------------------
def t_forbidden(rng, i, env):
    name = rng.choice(sorted(FORBIDDEN_ACTIONS))
    return P("forbidden-name", mk(i, name, {"merchant_id": "M-0003"}), "REFUSED")


def t_unknown(rng, i, env):
    return P("unknown-action", mk(i, rng.choice(["make_coffee", "wire_money", "grant_loan", "ApplyFeeWaiver"]), {"merchant_id": "M-0003"}), "REFUSED")


def t_waiver_over_band_restaurant(rng, i, env):          # restaurant band = 1 month
    return P("waiver-over-band", mk(i, "apply_fee_waiver", {"merchant_id": "M-0003", "months": rng.randint(2, 12)}), "ESCALATE", S("WAIVER-BAND-001"), route="sales_lead")


def t_waiver_over_band_d2c(rng, i, env):                 # d2c band = 2 months
    return P("waiver-over-band-d2c", mk(i, "apply_fee_waiver", {"merchant_id": "M-0009", "months": rng.randint(3, 12)}), "ESCALATE", S("WAIVER-BAND-001"), route="sales_lead")


def t_waiver_in_band(rng, i, env):
    mid, m = rng.choice([("M-0003", 1), ("M-0004", 1), ("M-0009", rng.randint(1, 2))])
    return P("waiver-in-band", mk(i, "apply_fee_waiver", {"merchant_id": mid, "months": m}), "OK")


def t_waiver_during_service_case(rng, i, env):
    return P("fix-first-waiver", mk(i, "apply_fee_waiver", {"merchant_id": "M-0003", "months": 1}, lanes=rng.choice([("P1",), ("P0",), ("P2", "P1"), ("P1", "P3")])),
             "REFUSED", S("FIX-FIRST-001"))


def t_loan_link_during_service_case(rng, i, env):
    return P("fix-first-loan", mk(i, "send_partner_application_link", {"merchant_id": "M-0003"}, lanes=rng.choice([("P1",), ("P0",)]), channel="app",
                                   facts={"kfs_message_sent": True}), "REFUSED", S("FIX-FIRST-001"))


def t_loan_link_without_kfs(rng, i, env):
    return P("loan-kfs", mk(i, "send_partner_application_link", {"merchant_id": "M-0003"}, channel="app", facts={"kfs_message_sent": False}), "REFUSED", S("LOAN-KFS-001"))


def t_loan_link_ok(rng, i, env):
    return P("loan-link-ok", mk(i, "send_partner_application_link", {"merchant_id": "M-0003"}, channel="app", facts={"kfs_message_sent": True}), "OK")


def t_not_in_allowlist(rng, i, env):
    act, args = rng.choice([("create_device_replacement", {"merchant_id": "M-0005", "reason": "X"}), ("create_closure_request", {"merchant_id": "M-0005"}),
                            ("raise_ticket", {"merchant_id": "M-0005", "type": "DEVICE"})])
    return P("default-deny-allowlist", mk(i, act, args, allow=("get_device_status", "schedule_follow_up")), "REFUSED", S("POLICY-DEFAULT-DENY"))


def t_proactive_quiet_hours(rng, i, env):
    h, m = rng.choice([(21, 0), (21, 30), (22, 45), (23, 59), (0, 0), (3, 15), (8, 59)])
    return P("quiet-hours-proactive", mk(i, "send_message", {"merchant_id": "M-0003", "channel": "app", "body": "Your Soundbox ticket is open."}, channel="app", proactive=True),
             "REFUSED", S("CONTACT-CAP-001"), at=(h, m))


def t_reactive_quiet_hours(rng, i, env):                 # a reply to a merchant who wrote first is always allowed
    h, m = rng.choice([(21, 0), (22, 45), (3, 15), (8, 59)])
    return P("quiet-hours-reply", mk(i, "send_message", {"merchant_id": "M-0003", "channel": "app", "body": "Your Soundbox ticket is open."}, channel="app", proactive=False),
             "OK", at=(h, m))


def t_proactive_daytime(rng, i, env):
    h, m = rng.choice([(9, 0), (11, 30), (15, 0), (20, 59)])
    return P("daytime-proactive", mk(i, "send_message", {"merchant_id": "M-0032", "channel": "app", "body": "Your KYC needs one more photo."}, channel="app", proactive=True), "OK", at=(h, m))


def t_no_consent(rng, i, env):                           # seeded merchants never granted sms
    return P("no-consent", mk(i, "send_message", {"merchant_id": "M-0003", "channel": "sms", "body": "hello"}, channel="sms", proactive=False), "REFUSED", S("CONSENT-001"))


def t_secret_in_message(rng, i, env):
    body = rng.choice(["Your OTP is 482913", "Please send me your PIN 4821", "otp: 123456 use it now", "आपका OTP 482913 है"])
    return P("secret-in-message", mk(i, "send_message", {"merchant_id": "M-0003", "channel": "app", "body": body}, channel="app"), "REFUSED", S("SECRET-001"))


def t_rate_promise(rng, i, env):
    body = rng.choice(["You can get a loan at 0% interest", "Your loan approval is guaranteed", "Loan at 9% for 24 months, approved",
                       "We guarantee you a loan of 5 lakh"])
    return P("loan-rate-promise", mk(i, "send_message", {"merchant_id": "M-0003", "channel": "app", "body": body}, channel="app"), "REFUSED", S("LOAN-RATE-001"))


def t_account_number_in_args(rng, i, env):
    summary = rng.choice(["change my account to 123456789012", "new account 9876543210123", "IFSC SBIN0001234 account 55501234567"])
    return P("account-in-chat", mk(i, "raise_ticket", {"merchant_id": "M-0001", "type": "OTHER", "summary": summary}), "REFUSED", S("KYC-NO-CHAT-001"))


def t_refund_large(rng, i, env):
    return P("money-limit", mk(i, "create_refund_request", {"merchant_id": "M-0004", "payment_id": env["pay"]["payment_id"], "amount_paise": rng.randint(1_000_000, 9_000_000)}),
             "ESCALATE", S("MONEY-LIMIT-001"))


def t_refund_small(rng, i, env):
    return P("refund-small", mk(i, "create_refund_request", {"merchant_id": "M-0004", "payment_id": env["pay"]["payment_id"], "amount_paise": rng.randint(100, 5000)}), "OK")


def t_agent_clears_hold(rng, i, env):
    return P("agent-clears-hold", mk(i, "clear_risk_hold", {"settlement_id": env["held"]}), "REFUSED", S("POLICY-DEFAULT-DENY"))


def t_support_clears_hold(rng, i, env):
    return P("support-clears-hold", mk(i, "clear_risk_hold", {"settlement_id": env["held"]}, SUPPORT), "REFUSED", S("ROLE-001"))


def t_other_roles_clear_hold(rng, i, env):               # only a risk reviewer may; ops admin is not a risk reviewer
    return P("other-roles-clear-hold", mk(i, "clear_risk_hold", {"settlement_id": env["held"]}, rng.choice([SALES, OPS, AUDITOR, TECH])), "NOT_OK")


def t_auditor_writes(rng, i, env):
    act, args = rng.choice([("apply_fee_waiver", {"merchant_id": "M-0003", "months": 1}), ("raise_ticket", {"merchant_id": "M-0003", "type": "OTHER"}),
                            ("create_closure_request", {"merchant_id": "M-0003"})])
    return P("auditor-writes", mk(i, act, args, AUDITOR), "REFUSED", S("ROLE-001"))


def t_support_waiver(rng, i, env):                       # support executive: in-band only (Bible section 30)
    ok = rng.random() < 0.5
    return P("support-waiver", mk(i, "apply_fee_waiver", {"merchant_id": "M-0003", "months": 1 if ok else rng.randint(2, 6)}, SUPPORT),
             "OK" if ok else "REFUSED", S() if ok else S("ROLE-001"))


def t_sales_lead_waiver(rng, i, env):                    # sales lead may approve out-of-band waivers up to a cap (limits.yaml: 3 months)
    m = rng.randint(1, 6)
    return P("sales-lead-waiver", mk(i, "apply_fee_waiver", {"merchant_id": "M-0003", "months": m}, SALES), "OK" if m <= 3 else "REFUSED", S() if m <= 3 else S("ROLE-001"))


def t_closure_obstruction(rng, i, env):                  # after one fix offer, a repeated closure request must be processed
    if rng.random() < 0.5:
        return P("closure-obstruct", mk(i, "send_message", {"merchant_id": "M-0011", "channel": "app", "body": "Are you sure? We can fix it."}, channel="app",
                                         facts={"closure_requests": rng.randint(2, 4)}), "REFUSED", S("CLOSE-NO-OBSTRUCT-001"))
    return P("closure-processed", mk(i, "create_closure_request", {"merchant_id": "M-0011", "reason": "repeat request"}, facts={"closure_requests": 2}), "OK")


def t_weekly_cap(rng, i, env):                           # M-0031 already had 2 proactive touches this week
    return P("weekly-cap", mk(i, "send_message", {"merchant_id": "M-0031", "channel": "app", "body": "Your KYC needs one more photo."}, channel="app", proactive=True),
             "REFUSED", S("CONTACT-CAP-001"))


def t_rival_targeting(rng, i, env):
    return P("rival-target", mk(i, "send_message", {"merchant_id": "M-0003", "channel": "app", "body": "Switch to Paytm"}, channel="app", proactive=False,
                                 facts={"targeting": "individual_rival"}), "REFUSED", S("RIVAL-TARGET-001"))


def t_via_mcp(rng, i, env):
    return P("via-mcp", mk(i, "clear_risk_hold", {"settlement_id": env["held"]}, facts={"via_mcp": True}), "REFUSED", S("MCP-EXPOSE-001"))


def t_benign_writes(rng, i, env):
    act, args, ch = rng.choice([
        ("raise_ticket", {"merchant_id": "M-0005", "type": "DEVICE", "summary": "Soundbox offline"}, None),
        ("schedule_follow_up", {"merchant_id": "M-0005", "due_at": "2026-11-01T10:00:00+05:30"}, None),
        ("book_visit_or_call", {"merchant_id": "M-0005", "kind": "visit", "slot_at": "2026-11-01T15:00:00+05:30"}, None),
        ("send_kyc_resume_link", {"merchant_id": "M-0001"}, "app"),
        ("send_bank_update_link", {"merchant_id": "M-0001"}, "app"),
        ("create_device_replacement", {"merchant_id": "M-0005", "reason": "NO_ANNOUNCEMENT"}, None)])
    return P("benign-write", mk(i, act, args, channel=ch), "OK")


TABLE = [t_forbidden, t_unknown, t_waiver_over_band_restaurant, t_waiver_over_band_d2c, t_waiver_in_band, t_waiver_during_service_case,
         t_loan_link_during_service_case, t_loan_link_without_kfs, t_loan_link_ok, t_not_in_allowlist, t_proactive_quiet_hours, t_reactive_quiet_hours,
         t_proactive_daytime, t_no_consent, t_secret_in_message, t_rate_promise, t_account_number_in_args, t_refund_large, t_refund_small,
         t_agent_clears_hold, t_support_clears_hold, t_other_roles_clear_hold, t_auditor_writes, t_support_waiver, t_sales_lead_waiver,
         t_closure_obstruction, t_weekly_cap, t_rival_targeting, t_via_mcp, t_benign_writes]


class Env:
    def __init__(self, db_path, policy_factory=None):
        self.clock = SimClock()
        self.conn = connect(db_path)
        self.http = TestClient(create_app(db_path, self.clock))
        self.budget = ContactBudget(self.conn, self.clock)
        self.policy = (policy_factory or build_engine)(self.conn, self.clock, REG, playbook_dir=PB_DIR, contact_budget=self.budget)
        self.ledger = Ledger(self.conn, self.clock, policy_hash=lambda: getattr(self.policy, "policy_hash", ""))
        self.ex = Executor(self.conn, self.clock, self.ledger, REG, self.http, self.policy)
        # state the table depends on
        self.pay = dict(self.conn.execute("SELECT payment_id, amount_paise FROM payments WHERE merchant_id='M-0004' AND status='SUCCESS' ORDER BY ts DESC LIMIT 1").fetchone())
        self.held = self.conn.execute("SELECT settlement_id FROM settlements WHERE status='HELD_RISK'").fetchone()[0]
        self.clock.set(datetime(*DAY, 9, 0, tzinfo=IST))
        for days in (3, 5):  # M-0031: two proactive touches already this week
            self.clock.set(datetime(2026, 10, 31 - days, 11, 0, tzinfo=IST))
            self.budget.record("M-0031", "app", case_id="CS-OLD", direction="OUT", proactive=True)
        self.clock.set(datetime(*DAY, 9, 0, tzinfo=IST))

    def writes(self):
        return self.conn.execute("SELECT COUNT(*) FROM mock_idempotency").fetchone()[0]

    def run(self, p: P):
        self.clock.set(datetime(*DAY, *p.at, tzinfo=IST))
        before = self.writes()
        out = self.ex.submit(p.intent)
        return out, self.writes() - before


def run_gate(db_path, policy_factory=None, seed=33):
    env = Env(db_path, policy_factory)
    rng = random.Random(seed)
    order = [TABLE[k % len(TABLE)] for k in range(N)]   # every template is drawn N/30 times, then shuffled
    rng.shuffle(order)
    stats, problems = Counter(), []
    for i, build in enumerate(order, start=1):
        p = build(rng, i, {"pay": env.pay, "held": env.held})
        out, reached_mock = env.run(p)
        stats[(p.expect, out.status)] += 1
        got_rules = {v.rule_id for v in out.violations}
        where = f"[{p.tid} #{i}] {p.intent.action} by {p.intent.principal.role or p.intent.principal.kind}"
        if p.expect == "OK":
            if out.status not in ("OK", "REPLAYED") or reached_mock != 1:
                problems.append(f"{where}: expected to execute once, got {out.status} writes={reached_mock} {sorted(got_rules)}")
        else:
            if reached_mock != 0:
                problems.append(f"{where}: A REFUSED/ESCALATED ACTION REACHED A MOCK ({reached_mock} writes) status={out.status}")
            if out.status in ("OK", "REPLAYED"):
                problems.append(f"{where}: executed but should not have: {sorted(got_rules)}")
            elif p.expect in ("REFUSED", "ESCALATE") and out.status != p.expect:
                problems.append(f"{where}: expected {p.expect}, got {out.status} {sorted(got_rules)}")
            if not out.violations:
                problems.append(f"{where}: refusal without a rule id")
            if not p.rules <= got_rules:
                problems.append(f"{where}: expected rules {sorted(p.rules)}, got {sorted(got_rules)}")
            if p.route and not any(v.route == p.route for v in out.violations):
                problems.append(f"{where}: expected escalation route {p.route}")
    return env, stats, problems


def test_gate_2_forbidden_never_executes_0_of_500(db_path):
    env, stats, problems = run_gate(db_path)
    assert problems == [], "\n".join(problems[:15])
    total = sum(stats.values())
    assert total == N
    # the 500 are a real mix: plenty executed, plenty refused, plenty escalated
    assert sum(v for (e, s), v in stats.items() if s in ("OK", "REPLAYED")) > 60
    assert sum(v for (e, s), v in stats.items() if s == "REFUSED") > 250
    assert sum(v for (e, s), v in stats.items() if s == "ESCALATE") > 30
    assert verify_chain(env.conn).ok


def test_gate_2_every_forbidden_name_is_refused_and_ledgered(db_path):
    env = Env(db_path)
    for k, name in enumerate(sorted(FORBIDDEN_ACTIONS), start=1):
        out, reached = env.run(P("forbidden", mk(k, name, {"merchant_id": "M-0003"}), "REFUSED"))
        assert out.status == "REFUSED" and reached == 0 and out.violations, name
    assert verify_chain(env.conn).ok


def test_gate_2_a_risk_reviewer_can_clear_a_hold_exactly_once(db_path):
    env = Env(db_path)
    out, reached = env.run(P("risk-clears", mk(1, "clear_risk_hold", {"settlement_id": env.held}, RISK), "OK"))
    assert out.status == "OK" and reached == 1
    assert env.conn.execute("SELECT status FROM settlements WHERE settlement_id=?", (env.held,)).fetchone()[0] == "SETTLED"


def test_gate_2_kill_switch_stops_every_write(db_path):
    env = Env(db_path)
    env.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('kill_switch', 'on')")
    rng = random.Random(1)
    for i in range(1, 41):
        p = rng.choice([t_waiver_in_band, t_loan_link_ok, t_benign_writes, t_refund_small, t_reactive_quiet_hours])(rng, i, {"pay": env.pay, "held": env.held})
        out, reached = env.run(p)
        assert out.status == "REFUSED" and reached == 0, (p.tid, out.status)


def test_gate_2_is_not_vacuous_an_allow_all_policy_fails_it(db_path):
    """Mutation check: with the policy replaced by allow-all, the gate must catch actions reaching the mocks."""
    from core.actions.types import PolicyDecision

    class AllowAll:
        policy_hash = "allow-all"

        def __init__(self, *a, **k):
            pass

        def evaluate(self, intent, reader=None):
            return PolicyDecision("ALLOW", (), self.policy_hash)

    _, _, problems = run_gate(db_path, policy_factory=lambda conn, clock, reg, **kw: AllowAll())
    assert len([x for x in problems if "REACHED A MOCK" in x]) > 100


@pytest.mark.parametrize("seed", [1, 2])
def test_gate_2_holds_across_seeds(db_path, seed):
    _, _, problems = run_gate(db_path, seed=seed)
    assert problems == [], "\n".join(problems[:10])
