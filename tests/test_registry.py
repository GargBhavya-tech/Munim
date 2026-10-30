import re

import pytest

from core.actions.registry import FORBIDDEN_ACTIONS, ActionRegistry, ActionSpec, ForbiddenAction, RegistryError, RiskClass, UnknownAction
from packs.paytm_merchant.actions import SPECS, Merchant, build_registry
from packs.paytm_merchant.mocks import create_app


@pytest.fixture(scope="module")
def reg():
    return build_registry()


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ACTIONS))
def test_forbidden_actions_do_not_exist(reg, name):
    with pytest.raises(ForbiddenAction):
        reg.get(name)
    assert not reg.has(name)
    assert name not in reg.names()


def test_registering_a_forbidden_action_is_refused():
    with pytest.raises(ForbiddenAction):
        ActionRegistry([ActionSpec("approve_kyc", RiskClass.R3, Merchant)])


def test_unknown_action_is_not_forbidden(reg):
    with pytest.raises(UnknownAction):
        reg.get("make_coffee")


def test_every_agent_callable_r1_r2_has_a_real_compensation(reg):
    for s in reg.specs():
        if s.cls in (RiskClass.R1, RiskClass.R2) and s.agent_callable:
            comp = reg.get(s.compensation)
            assert not comp.agent_callable, f"{s.name}: the compensation must not be offered to the model"
            assert s.comp_args is not None


def test_irreversible_actions_have_no_compensation(reg):
    assert all(s.compensation is None for s in reg.specs() if s.cls is RiskClass.R3)


def test_reads_are_gets_and_writes_are_not(reg):
    for s in reg.specs():
        if s.cls is RiskClass.R0:
            assert s.method == "GET"
        if s.cls in (RiskClass.R1, RiskClass.R2, RiskClass.R3) and s.system:
            assert s.method in ("POST", "DELETE")


def test_validation_rejects_bad_registries():
    with pytest.raises(RegistryError, match="no registered compensation"):
        ActionRegistry([ActionSpec("x", RiskClass.R2, Merchant, "tickets", "POST", "/mock/tickets")])
    with pytest.raises(RegistryError, match="without a method"):
        ActionRegistry([ActionSpec("x", RiskClass.R0, Merchant, "tickets")])
    with pytest.raises(RegistryError, match="read but uses"):
        ActionRegistry([ActionSpec("x", RiskClass.R0, Merchant, "tickets", "POST", "/mock/tickets")])
    with pytest.raises(RegistryError, match="duplicate"):
        ActionRegistry([ActionSpec("x", RiskClass.DRAFT, Merchant), ActionSpec("x", RiskClass.DRAFT, Merchant)])


def test_every_registered_path_is_a_real_mock_endpoint(reg, db_path):
    openapi = create_app(db_path).openapi()["paths"]
    for s in reg.specs():
        if not s.system:
            continue
        template = re.sub(r"\{[^}]+\}", "{}", s.path.split("?")[0])
        shapes = {re.sub(r"\{[^}]+\}", "{}", p): {m.upper() for m in ops} for p, ops in openapi.items()}
        assert template in shapes, f"{s.name}: {s.path} is not a mock endpoint"
        assert s.method in shapes[template], f"{s.name}: {s.method} not allowed on {s.path}"


def test_path_and_body_fields_exist_on_the_args_model(reg):
    for s in reg.specs():
        fields = set(s.args_model.model_fields)
        for placeholder in re.findall(r"\{(\w+)\}", s.path or ""):
            assert placeholder in fields, f"{s.name}: {{{placeholder}}} is not an argument"
        for f in s.body_fields or ():
            assert f in fields, f"{s.name}: body field {f} is not an argument"


def test_compensation_args_build_a_valid_compensation_call(reg):
    samples = {
        "raise_ticket": ({"merchant_id": "M-0001", "type": "DEVICE"}, {"ticket_id": "T-0009"}),
        "create_device_replacement": ({"merchant_id": "M-0001"}, {"replacement_id": "R-0009"}),
        "apply_fee_waiver": ({"merchant_id": "M-0001", "months": 1}, {"waiver_id": "W-0009"}),
        "create_plan_change": ({"merchant_id": "M-0001", "to_plan_id": "BASIC"}, {"change_id": "PC-0001"}),
        "schedule_follow_up": ({"merchant_id": "M-0001", "due_at": "x"}, {"schedule_id": "SCH-0001"}),
    }
    for name, (args, result) in samples.items():
        spec = reg.get(name)
        comp = reg.get(spec.compensation)
        built = spec.comp_args(args, result)
        comp.args_model.model_validate(built)  # raises if the shape is wrong


def test_offer_and_contact_sets():
    reg = build_registry()
    assert reg.offer_actions() == {"apply_fee_waiver", "create_device_order", "create_plan_change", "send_partner_application_link"}
    assert {"send_message", "send_kyc_resume_link", "send_bank_update_link", "send_partner_application_link"} <= reg.contact_actions()
    assert not (reg.offer_actions() & {s.name for s in SPECS if s.cls is RiskClass.R0})


def test_firmware_and_human_only_actions_are_not_agent_callable(reg):
    assert "update_firmware" not in reg.names()
    assert not reg.get("clear_risk_hold").agent_callable
    assert "clear_risk_hold" not in reg.names(agent_only=True)
