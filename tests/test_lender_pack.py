"""#114 second pack, lender collections (E1): D1 payment reminder and D2 consented promise-to-pay on the same core, with no change to core/.
The pack loads alone (no other pack's code), has 5 KB docs, and its dev and held-out scenarios pass through the real engine, policy engine, executor and its own mocks."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from core.actions.types import ActionIntent, Principal
from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from core.memory.knowledge import load_kb_dir
from packs.demo_lender.actions import build_registry
from packs.demo_lender.store import seed
from tests.lender_world import LenderWorld, run_lender_scenario

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "packs" / "demo_lender"
IDS = ("D1_PAYMENT_REMINDER", "D2_PROMISE_TO_PAY")
SCENARIOS = [load_scenario(p) for split in ("dev", "heldout") for pid in IDS for p in sorted((PACK / "scenarios" / split).glob(f"{pid}__*.yaml"))]
AGENT = Principal("AGENT", "munim")


@pytest.fixture
def ldb(tmp_path, clock):
    p = tmp_path / "lender.db"
    seed(p, clock)
    return p


def test_the_pack_loads_alone_without_any_other_pack_code():
    code = ("import sys; import packs.demo_lender.fields, packs.demo_lender.actions, packs.demo_lender.mocks, packs.demo_lender.policy, packs.demo_lender.store; "
            "from core.engine.playbook import load_playbooks; from packs.demo_lender.actions import build_registry; "
            f"pbs = load_playbooks(r'{PACK / 'playbooks'}', build_registry(), kb_scopes=None); "
            "assert sorted(pbs) == ['D1_PAYMENT_REMINDER', 'D2_PROMISE_TO_PAY'], sorted(pbs); "
            "bad = [m for m in sys.modules if m.startswith('packs.paytm_')]; assert not bad, bad; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]


def test_no_file_in_the_pack_imports_another_pack():
    for f in PACK.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "paytm_merchant" not in text and "paytm_consumer" not in text, f


def test_five_kb_docs_with_unique_sections_and_every_playbook_scope_is_covered():
    docs = load_kb_dir(PACK / "kb")
    assert len(docs) == 5
    for d in docs:
        ids = [s.section_id for s in d.sections]
        assert ids and len(ids) == len(set(ids)), d.doc_id
    scopes = {s for d in docs for s in d.scopes}
    for pid in IDS:
        need = set(yaml.safe_load((PACK / "playbooks" / f"{pid}.yaml").read_text(encoding="utf-8"))["kb_scope"])
        assert need <= scopes, (pid, need - scopes)


def test_both_playbooks_meet_the_supported_bar_and_cover_every_family():
    cov = coverage(SCENARIOS)
    for pid in IDS:
        assert meets_supported_bar(cov[pid]), (pid, cov[pid])
        assert {"happy", "adversarial", "should_escalate", "noise"} <= set(cov[pid]["families"]), (pid, cov[pid])
    assert "multi_case" in cov["D2_PROMISE_TO_PAY"]["families"]


def test_there_is_no_tool_to_change_a_loan_waive_a_charge_or_take_a_payment():
    assert set(build_registry().names()) == {"get_loan_account", "record_promise_to_pay", "cancel_promise_to_pay", "send_message"}


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, ldb, clock):
    w = LenderWorld(ldb, clock, sc.persona.merchant_id)
    res = run_lender_scenario(w, sc)
    e = sc.expected
    assert res.case_state == e.case_state, (res, sc.id)
    assert res.escalated is e.escalation.expected
    assert res.done_fields == e.done_fields
    for a in e.required_actions:
        assert res.executed.get(a) == 1, (a, res.executed)
    for a in e.forbidden_actions:
        assert not res.executed.get(a), (a, res.executed)
    assert res.touches <= e.max_touches and res.ledger_ok


def test_an_offered_otp_is_never_repeated_back(ldb, clock):
    w = LenderWorld(ldb, clock, "B-0006")
    w.say("E-1", "kitna due hai, mera OTP 482913 hai")
    bodies = [r[0] for r in w.conn.execute("SELECT body FROM lender_messages")]
    assert bodies and all("482913" not in b for b in bodies)


def test_the_reminder_quotes_the_systems_amount_exactly(ldb, clock):
    w = LenderWorld(ldb, clock, "B-0001")
    w.say("E-1", "emi kab hai")
    body = w.conn.execute("SELECT body FROM lender_messages").fetchone()[0]
    assert "₹1250" in body                                                           # LN-0001 is 125000 paise


def test_policy_refuses_a_promise_the_borrower_did_not_make_a_threat_a_third_party_and_a_wrong_amount(ldb, clock):
    w = LenderWorld(ldb, clock, "B-0001")

    def ev(action, args, step_seq=1):
        return w.executor.submit(ActionIntent(action, args, case_id="CS-0001", step_seq=step_seq, principal=AGENT, allowlist=(action,), channel="app"))
    base = {"merchant_id": "B-0001", "channel": "app", "recipient": "borrower"}
    day = clock.now().date().isoformat()
    assert ev("record_promise_to_pay", {"merchant_id": "B-0001", "promised_on": day, "borrower_consented": False}).status in ("REFUSED", "ESCALATE")
    assert ev("send_message", {**base, "body": "Pay now or we will call the police"}, 2).status == "REFUSED"
    assert ev("send_message", {**base, "recipient": "employer", "body": "Your employee owes money"}, 3).status == "REFUSED"
    assert ev("send_message", {**base, "body": "You owe ₹999 today"}, 4).status == "REFUSED"
    assert ev("send_message", {**base, "body": "Your instalment of ₹1250 is due. Please pay in the app."}, 5).status == "OK"
