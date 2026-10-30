"""diagnose / done_check against the seeded DB through a Reader that wraps the mock app.

The reader helper lives here on purpose: shared code gets the real Reader from the executor engineer.
"""
from pathlib import Path

import pytest

from core.actions.registry import RiskClass
from core.engine.diagnose import diagnose, done_check, escalation_reasons
from core.engine.playbook import load_playbooks
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.mocks.client import MockClient

PACK = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant"


@pytest.fixture(scope="module")
def registry():
    return build_registry()


@pytest.fixture(scope="module")
def pbs(registry):
    return load_playbooks(PACK / "playbooks", registry)


class CountingReader:
    def __init__(self, http, registry):
        self.mc, self.registry, self.calls = MockClient(http), registry, []

    def __call__(self, name, args):
        spec = self.registry.get(name)
        assert spec.cls is RiskClass.R0, "diagnose may only call reads"
        a = spec.args_model(**args).model_dump()
        self.calls.append(name)
        return self.mc.get(spec.path.format(**a)).body


@pytest.fixture
def reader(http, registry):
    return CountingReader(http, registry)


def flip(http, kind, mid, status, reason=None):
    r = http.post(f"/mock/control/{kind}/{mid}", json={"status": status, "reason": reason})
    assert r.status_code == 200, r.text


def pay_and_settle(mock, http, mid):
    pay = mock.write("POST", "/mock/payments/simulate", key=f"pay-{mid}", json={"merchant_id": mid, "amount_paise": 24000, "status": "SUCCESS"})
    sid = pay.body["settlement_id"]
    assert mock.write("POST", f"/mock/settlements/{sid}/advance", key=f"adv-{mid}", json={"to": "SETTLED"}).status == 200
    return sid


# ---- S1: the ticket's flip test (M-0001: KYC rejected, bank failed, device offline) ----------------------
def test_s1_returns_kyc_then_bank_then_device_then_payment_then_settlement_then_none(pbs, reader, http, mock):
    s1, mid = pbs["S1_ACTIVATION"], "M-0001"
    assert diagnose(s1, reader, mid, {}).name == "kyc"
    flip(http, "kyc", mid, "APPROVED")
    assert diagnose(s1, reader, mid, {}).name == "bank"
    flip(http, "bank", mid, "VERIFIED")
    assert diagnose(s1, reader, mid, {}).name == "device"
    flip(http, "device", mid, "ONLINE")
    # M-0001 has seeded history: latest payment SUCCESS, its settlement S-0030 still PENDING
    assert diagnose(s1, reader, mid, {}).name == "settlement"
    assert done_check(s1, reader, mid, {}) is False
    mock.write("POST", "/mock/settlements/S-0030/advance", key="adv-S-0030", json={"to": "SETTLED"})
    assert diagnose(s1, reader, mid, {}) is None
    assert done_check(s1, reader, mid, {}) is True


def test_s1_order_is_fixed_even_when_a_later_step_is_the_only_one_done(pbs, reader, http):
    s1, mid = pbs["S1_ACTIVATION"], "M-0001"
    flip(http, "device", mid, "ONLINE")       # a later step is satisfied; the first unmet is still KYC
    assert diagnose(s1, reader, mid, {}).name == "kyc"


# ---- M-0006: the seeded stalled signup, S1 end to end ------------------------------------------------------
def test_s1_on_seeded_stalled_signup_m0006(pbs, reader, http, mock):
    s1, mid = pbs["S1_ACTIVATION"], "M-0006"
    assert diagnose(s1, reader, mid, {}).name == "kyc"          # REJECTED(DOC_BLURRY)
    assert done_check(s1, reader, mid, {}) is False
    flip(http, "kyc", mid, "APPROVED")
    assert diagnose(s1, reader, mid, {}).name == "bank"         # PENDING
    assert mock.write("POST", f"/mock/bank/{mid}/retrigger", key="rt-6").body["status"] == "VERIFIED"
    assert diagnose(s1, reader, mid, {}).name == "device"       # OFFLINE
    flip(http, "device", mid, "ONLINE")
    assert diagnose(s1, reader, mid, {}).name == "payment"      # no payments yet
    pay = mock.write("POST", "/mock/payments/simulate", key="p-6", json={"merchant_id": mid, "amount_paise": 24000, "status": "SUCCESS"})
    assert diagnose(s1, reader, mid, {}).name == "settlement"   # payment SUCCESS, its settlement still PENDING
    assert done_check(s1, reader, mid, {}) is False
    mock.write("POST", f"/mock/settlements/{pay.body['settlement_id']}/advance", key="a-6", json={"to": "SETTLED"})
    assert diagnose(s1, reader, mid, {}) is None
    assert done_check(s1, reader, mid, {}) is True


def test_s1_settlement_must_be_the_one_for_the_latest_payment(pbs, reader, http, mock):
    """An older settled settlement must not satisfy 'that payment SETTLED'."""
    s1, mid = pbs["S1_ACTIVATION"], "M-0006"
    flip(http, "kyc", mid, "APPROVED"); flip(http, "bank", mid, "VERIFIED"); flip(http, "device", mid, "ONLINE")
    pay_and_settle(mock, http, mid)                              # first payment fully settled
    assert diagnose(s1, reader, mid, {}) is None
    mock.write("POST", "/mock/payments/simulate", key="p-6b", json={"merchant_id": mid, "amount_paise": 5000, "status": "SUCCESS"})
    step = diagnose(s1, reader, mid, {})
    assert step is not None and step.name == "settlement"        # new payment, new PENDING settlement


# ---- C2 on the seeded offline device M-0005 ---------------------------------------------------------------
def test_c2_on_seeded_offline_device_m0005(pbs, reader, http):
    c2, mid = pbs["C2_SOUNDBOX_FAULT"], "M-0005"
    assert diagnose(c2, reader, mid, {}).name == "check_basics"
    assert done_check(c2, reader, mid, {}) is False
    facts = {"steps": {"check_basics": {"done": True}}}
    assert diagnose(c2, reader, mid, facts).name == "remote_fix"             # a remote fix comes before the announcement test (#93)
    assert http.post(f"/mock/devices/{mid}/restart", headers={"Idempotency-Key": "diag-1"}).status_code == 200
    assert diagnose(c2, reader, mid, facts).name == "test_announcement"      # one remote attempt, even a failed one, uses the step up
    flip(http, "device", mid, "ONLINE")                          # control also sets the announcement flag
    assert diagnose(c2, reader, mid, facts) is None
    assert done_check(c2, reader, mid, facts) is True


def test_c2_online_device_needs_no_basics_check(pbs, reader, http):
    c2, mid = pbs["C2_SOUNDBOX_FAULT"], "M-0005"
    flip(http, "device", mid, "ONLINE")
    assert diagnose(c2, reader, mid, {}) is None


def test_c2_flat_dotted_case_fact_works_too(pbs, reader):
    assert diagnose(pbs["C2_SOUNDBOX_FAULT"], reader, "M-0005", {"steps.check_basics.done": True}).name == "remote_fix"


def test_c2_escalation_reasons_use_live_replacement_count_and_case_intent(pbs, reader):
    c2 = pbs["C2_SOUNDBOX_FAULT"]
    assert escalation_reasons(c2, reader, "M-0005", {}) == []
    assert escalation_reasons(c2, reader, "M-0012", {}) == ["replacements_last_30d >= 1"]   # chronic device
    assert escalation_reasons(c2, reader, "M-0005", {"intent": "DEVICE_STOLEN"}) == ["intent in ['DEVICE_STOLEN', 'DEVICE_TAMPERED']"]


def test_s1_escalation_on_second_kyc_rejection(pbs, reader, http):
    s1, mid = pbs["S1_ACTIVATION"], "M-0006"
    assert escalation_reasons(s1, reader, mid, {}) == []         # attempts == 1
    http.post(f"/mock/control/kyc/{mid}", json={"status": "REJECTED", "reason": "NAME_MISMATCH"})
    assert escalation_reasons(s1, reader, mid, {"intent": "CHANGE_BANK"}) == ["intent in ['USE_ANOTHER_ACCOUNT', 'CHANGE_BANK']"]


# ---- the Watch out: no caching ---------------------------------------------------------------------------
def test_every_call_rereads_every_required_read(pbs, reader):
    s1 = pbs["S1_ACTIVATION"]
    diagnose(s1, reader, "M-0006", {})
    assert sorted(reader.calls) == sorted(s1.required_reads)
    n = len(reader.calls)
    diagnose(s1, reader, "M-0006", {})
    done_check(s1, reader, "M-0006", {})
    assert len(reader.calls) == n + 2 * len(s1.required_reads)


def test_no_caching_across_calls_state_flip_changes_the_answer(pbs, reader, http):
    s1, mid = pbs["S1_ACTIVATION"], "M-0006"
    first = diagnose(s1, reader, mid, {})
    flip(http, "kyc", mid, "APPROVED")
    second = diagnose(s1, reader, mid, {})
    assert (first.name, second.name) == ("kyc", "bank")
    flip(http, "kyc", mid, "REJECTED", "DOC_BLURRY")             # and back: a stale cache would say "bank"
    assert diagnose(s1, reader, mid, {}).name == "kyc"


def test_done_check_is_not_cached_either(pbs, reader, http):
    c2, mid = pbs["C2_SOUNDBOX_FAULT"], "M-0005"
    assert done_check(c2, reader, mid, {}) is False
    flip(http, "device", mid, "ONLINE")
    assert done_check(c2, reader, mid, {}) is True
    flip(http, "device", mid, "OFFLINE")
    assert done_check(c2, reader, mid, {}) is False


def test_diagnose_source_has_no_cache():
    src = (Path(__file__).resolve().parent.parent / "core/engine/diagnose.py").read_text()
    assert "lru_cache" not in src and "functools" not in src
    fsrc = (Path(__file__).resolve().parent.parent / "core/engine/fields.py").read_text()
    assert "lru_cache" not in fsrc and "_cache" not in fsrc
