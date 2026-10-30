"""#89 consumer pack: C11 (UPI debited, payee not credited) and C12 (refund not received) on the same core. The pack loads alone
(no merchant-pack code), and its dev and held-out scenarios pass through the real engine, policy engine, executor and its own mocks."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from packs.paytm_consumer.store import seed
from tests.consumer_world import ConsumerWorld, run_consumer_scenario

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "packs" / "paytm_consumer"
SC_DIR = Path(os.environ.get("MUNIM_CONSUMER_SCENARIO_DIR") or PACK / "scenarios")
IDS = ("C11_UPI_DEBITED_FAILED", "C12_REFUND_NOT_RECEIVED")
SCENARIOS = [load_scenario(p) for split in ("dev", "heldout") for pid in IDS for p in sorted((SC_DIR / split).glob(f"{pid}__*.yaml"))]


@pytest.fixture
def cdb(tmp_path, clock):
    p = tmp_path / "consumer.db"
    seed(p, clock)
    return p


def test_the_pack_loads_alone_without_any_merchant_pack_code():
    code = ("import sys; import packs.paytm_consumer.fields, packs.paytm_consumer.actions, packs.paytm_consumer.mocks, packs.paytm_consumer.policy, "
            "packs.paytm_consumer.store; from core.engine.playbook import load_playbooks; from packs.paytm_consumer.actions import build_registry; "
            f"pbs = load_playbooks(r'{PACK / 'playbooks'}', build_registry(), kb_scopes=set()); "
            "assert sorted(pbs) == ['C11_UPI_DEBITED_FAILED', 'C12_REFUND_NOT_RECEIVED'], sorted(pbs); "
            "bad = [m for m in sys.modules if m.startswith('packs.paytm_merchant')]; assert not bad, bad; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]


def test_no_file_in_the_pack_imports_the_merchant_pack():
    for f in PACK.rglob("*.py"):
        assert "paytm_merchant" not in f.read_text(encoding="utf-8"), f


def test_both_playbooks_meet_the_supported_bar_and_cover_every_family():
    cov = coverage(SCENARIOS)
    for pid in IDS:
        assert meets_supported_bar(cov[pid]), (pid, cov[pid])
        assert cov[pid]["families"] == ["adversarial", "happy", "multi_case", "noise", "should_escalate"], (pid, cov[pid])


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, cdb, clock):
    w = ConsumerWorld(cdb, clock, sc.persona.merchant_id)
    res = run_consumer_scenario(w, sc)
    e = sc.expected
    assert res.case_state == e.case_state, (res, sc.id)
    assert res.escalated is e.escalation.expected
    assert res.done_fields == e.done_fields
    for a in e.required_actions:
        assert res.executed.get(a) == 1, (a, res.executed)
    for a in e.forbidden_actions:
        assert not res.executed.get(a), (a, res.executed)
    assert res.touches <= e.max_touches and res.ledger_ok


def test_an_offered_otp_is_never_repeated_back(cdb, clock):
    w = ConsumerWorld(cdb, clock, "U-0001")
    w.say("E-1", "paise kat gaye, mera OTP 482913 le lo")
    bodies = [r[0] for r in w.conn.execute("SELECT body FROM consumer_messages")]
    assert bodies and all("482913" not in b for b in bodies)


def test_the_consumer_secret_rule_blocks_an_otp_in_a_message(cdb, clock):
    from core.actions.types import ActionIntent
    w = ConsumerWorld(cdb, clock, "U-0001")
    out = w.executor.submit(ActionIntent("send_message", {"merchant_id": "U-0001", "channel": "app", "body": "Your OTP 482913 is safe with us"},
                                         case_id="CS-T", step_seq=1, allowlist=("send_message",), channel="app"))
    assert out.status == "REFUSED" and [v.rule_id for v in out.violations] == ["C-SECRET-001"]


def test_a_dispute_is_raised_once_even_if_the_mock_is_asked_twice(cdb, clock):
    from core.actions.types import ActionIntent
    w = ConsumerWorld(cdb, clock, "U-0001")
    for seq in (1, 2):
        w.executor.submit(ActionIntent("raise_upi_dispute", {"merchant_id": "U-0001", "txn_id": "T-0001"}, case_id=f"CS-{seq}", step_seq=1,
                                       allowlist=("raise_upi_dispute",)))
    assert w.conn.execute("SELECT COUNT(*) FROM consumer_disputes WHERE txn_id = 'T-0001' AND status = 'OPEN'").fetchone()[0] == 1
