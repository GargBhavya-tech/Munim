from pathlib import Path

import pytest

from core.engine import playbook as pbmod
from core.engine.playbook import (
    ExpressionError, ForbiddenActionError, Playbook, PlaybookError, SchemaError, UnknownActionError,
    UnknownFieldError, UnknownKBScopeError, kb_scopes_from_dir, load_playbook, load_playbooks,
)
from packs.paytm_merchant.actions import build_registry

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "playbooks"
PACK = ROOT / "packs" / "paytm_merchant"
KB_SCOPES = kb_scopes_from_dir(PACK / "kb")


@pytest.fixture(scope="module")
def registry():
    return build_registry()


def load(name, registry, scopes=KB_SCOPES):
    return load_playbook(FIX / name, registry, scopes)


def test_kb_scopes_come_from_front_matter():
    assert {"device_troubleshooting", "kyc_faq", "safety_lines", "device_replacement_policy"} <= KB_SCOPES
    assert "secret_pricing_notes" not in KB_SCOPES


def test_valid_playbook_loads_with_every_field(registry):
    pb = load("valid_minimal.yaml", registry)
    assert isinstance(pb, Playbook)
    assert (pb.id, pb.half, pb.priority_lane) == ("T_FIXTURE", "service", "P1")
    assert pb.triggers.events == ["DEVICE_HEARTBEAT_LOST"] and pb.triggers.intents == ["REPORT_DEVICE_PROBLEM"]
    assert pb.required_reads == ["get_device_status"]
    assert (pb.max_touches, pb.sla_sim_hours, pb.alt_terminal) == (3, 48, None)
    assert pb.kb_scope == ["device_troubleshooting"]


def test_bare_string_step_defaults_done_to_steps_name_done(registry):
    pb = load("valid_minimal.yaml", registry)
    assert [(s.name, s.done) for s in pb.steps] == [
        ("check_basics", "steps.check_basics.done"),
        ("confirm", "device.status == 'ONLINE'"),
    ]


def test_allowed_action_forms_are_normalised(registry):
    pb = load("valid_minimal.yaml", registry)
    assert [a.name for a in pb.allowed_actions] == ["book_visit_or_call", "create_device_replacement", "raise_ticket"]
    rep = pb.action("create_device_replacement")
    assert rep.max_per_case == 1
    assert rep.precondition == "steps.check_basics.done and device.status == 'OFFLINE'"
    assert pb.action("raise_ticket").constraints == {"type": "DEVICE"}
    assert pb.action("book_visit_or_call").max_per_case is None


def test_pack_playbooks_load_with_default_kb_scopes(registry):
    pbs = load_playbooks(PACK / "playbooks", registry)
    assert {"S1_ACTIVATION", "C2_SOUNDBOX_FAULT"} <= set(pbs)
    s1, c2 = pbs["S1_ACTIVATION"], pbs["C2_SOUNDBOX_FAULT"]
    assert [s.name for s in s1.steps] == ["kyc", "bank", "device", "payment", "settlement"]
    assert [s.name for s in c2.steps] == ["check_basics", "remote_fix", "test_announcement", "replace_or_visit"]
    assert c2.action("create_device_replacement").max_per_case == 1
    assert c2.escalate_when == ["replacements_last_30d >= 1", "intent in ['DEVICE_STOLEN', 'DEVICE_TAMPERED']"]
    assert c2.done_check == "device.status == 'ONLINE' and last_test_announcement_ok"
    assert (c2.max_touches, c2.sla_sim_hours, c2.priority_lane) == (3, 48, "P1")
    assert s1.priority_lane == "P2" and s1.half == "sales"


def test_forbidden_action_approve_kyc_is_rejected_with_forbidden_action_code(registry):
    with pytest.raises(ForbiddenActionError) as e:
        load("bad_forbidden_approve_kyc.yaml", registry)
    assert e.value.code == "FORBIDDEN_ACTION"
    assert "FORBIDDEN_ACTION" in str(e.value) and "approve_kyc" in str(e.value)


def test_forbidden_action_in_mapping_form_is_also_rejected(registry):
    with pytest.raises(ForbiddenActionError):
        load("bad_forbidden_in_mapping_form.yaml", registry)


def test_every_forbidden_action_is_rejected(registry):
    from core.actions.registry import FORBIDDEN_ACTIONS
    base = (FIX / "valid_minimal.yaml").read_text()
    import yaml
    for name in sorted(FORBIDDEN_ACTIONS):
        data = yaml.safe_load(base)
        data["allowed_actions"].append(name)
        with pytest.raises(ForbiddenActionError):
            pbmod.parse_playbook(data, registry, KB_SCOPES)


def test_unknown_action_is_rejected(registry):
    with pytest.raises(UnknownActionError) as e:
        load("bad_unknown_action.yaml", registry)
    assert "teleport_money" in str(e.value)
    assert not isinstance(e.value, ForbiddenActionError)


def test_compensation_actions_are_not_grantable_to_a_playbook(registry):
    with pytest.raises(UnknownActionError):
        load("bad_compensation_as_action.yaml", registry)


def test_required_read_must_be_a_registered_read(registry):
    with pytest.raises(UnknownActionError):
        load("bad_write_as_required_read.yaml", registry)


def test_unknown_kb_scope_is_rejected(registry):
    with pytest.raises(UnknownKBScopeError) as e:
        load("bad_unknown_kb_scope.yaml", registry)
    assert "secret_pricing_notes" in str(e.value)


def test_unknown_done_check_field_is_rejected(registry):
    with pytest.raises(UnknownFieldError) as e:
        load("bad_unknown_done_field.yaml", registry)
    assert "device.colour" in str(e.value)


def test_import_in_done_check_fails_to_parse(registry):
    with pytest.raises(ExpressionError) as e:
        load("bad_import_done_check.yaml", registry)
    assert e.value.code == "BAD_EXPRESSION"


def test_attribute_trick_in_done_check_is_rejected(registry):
    with pytest.raises(ExpressionError):
        load("bad_attribute_trick_done_check.yaml", registry)


def test_call_in_a_precondition_is_rejected(registry):
    with pytest.raises(ExpressionError):
        load("bad_precondition_call.yaml", registry)


def test_field_whose_read_is_not_declared_is_rejected(registry):
    with pytest.raises(UnknownFieldError) as e:
        load("bad_field_without_read.yaml", registry)
    assert "get_device_status" in str(e.value)


@pytest.mark.parametrize("name", ["bad_duplicate_step.yaml", "bad_unknown_key.yaml", "bad_lane.yaml"])
def test_schema_violations_are_rejected(registry, name):
    with pytest.raises(SchemaError):
        load(name, registry)


def test_unknown_step_reference_in_precondition_is_rejected(registry):
    import yaml
    data = yaml.safe_load((FIX / "valid_minimal.yaml").read_text())
    data["allowed_actions"][1]["create_device_replacement"]["precondition"] = "steps.nope.done"
    with pytest.raises(UnknownFieldError):
        pbmod.parse_playbook(data, registry, KB_SCOPES)


def test_non_mapping_yaml_is_rejected(registry, tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(SchemaError):
        load_playbook(p, registry, KB_SCOPES)


def test_load_playbooks_rejects_duplicate_ids(registry, tmp_path):
    for n in ("a.yaml", "b.yaml"):
        (tmp_path / n).write_text((FIX / "valid_minimal.yaml").read_text())
    with pytest.raises(SchemaError):
        load_playbooks(tmp_path, registry, KB_SCOPES)


def test_one_bad_playbook_in_a_directory_fails_the_whole_load(registry, tmp_path):
    (tmp_path / "ok.yaml").write_text((FIX / "valid_minimal.yaml").read_text().replace("T_FIXTURE", "T_OK"))
    (tmp_path / "bad.yaml").write_text((FIX / "bad_forbidden_approve_kyc.yaml").read_text())
    with pytest.raises(PlaybookError):
        load_playbooks(tmp_path, registry, KB_SCOPES)


def test_loader_has_no_python_eval(tmp_path):
    import re
    text = (ROOT / "core/engine/playbook.py").read_text()
    assert not re.search(r"(?<![\w.])(eval|exec)\s*\(", text)
