import builtins
import re
from pathlib import Path

import pytest

from core.engine.safe_expr import ExpressionError, UnknownFieldError, compile_expr

FIELDS = {"kyc.status", "device.status", "device.replacements_last_30d", "touches", "intent", "steps.a.done", "ok", "x"}


def ev(src, ns=None):
    return compile_expr(src, FIELDS).evaluate(ns or {})


def test_comparisons_and_boolean_logic_give_concrete_answers():
    ns = {"kyc.status": "APPROVED", "device.status": "OFFLINE", "touches": 3}
    assert ev("kyc.status == 'APPROVED'", ns) is True
    assert ev("kyc.status != 'APPROVED'", ns) is False
    assert ev("kyc.status == 'APPROVED' and device.status == 'ONLINE'", ns) is False
    assert ev("device.status == 'ONLINE' or touches >= 3", ns) is True
    assert ev("not (touches < 3)", ns) is True
    assert ev("touches >= 3 and touches <= 3", ns) is True
    assert ev("1 < touches < 5", ns) is True
    assert ev("1 < touches < 3", ns) is False


def test_in_and_not_in_over_literal_lists():
    ns = {"intent": "DEVICE_STOLEN"}
    assert ev("intent in ['DEVICE_STOLEN', 'DEVICE_TAMPERED']", ns) is True
    assert ev("intent not in ['DEVICE_STOLEN']", ns) is False
    assert ev("intent in ('A', 'B')", ns) is False


def test_dotted_names_resolve_and_fields_are_reported():
    e = compile_expr("steps.a.done and device.replacements_last_30d >= 1", FIELDS)
    assert e.fields == {"steps.a.done", "device.replacements_last_30d"}
    assert e.evaluate({"steps.a.done": True, "device.replacements_last_30d": 1}) is True


def test_missing_field_is_none_and_incomparable_values_are_false_not_errors():
    assert ev("device.replacements_last_30d >= 1", {}) is False
    assert ev("touches >= 1", {"touches": "three"}) is False
    assert ev("ok", {}) is None
    assert not ev("steps.a.done", {})


def test_unknown_field_is_rejected_with_its_name():
    with pytest.raises(UnknownFieldError) as e:
        compile_expr("device.colour == 'RED'", FIELDS)
    assert e.value.field == "device.colour"
    with pytest.raises(UnknownFieldError):
        compile_expr("kyc.status.value == 'x'", FIELDS)


ATTACKS = [
    "__import__('os')",
    "__import__('os').system('id')",
    "().__class__",
    "().__class__.__bases__[0].__subclasses__()",
    "kyc.status.__class__",
    "kyc.status.upper()",
    "'a'.join(['b'])",
    "lambda: 1",
    "(lambda x: x)(1)",
    "[x for x in range(3)]",
    "{x for x in 'ab'}",
    "(x for x in 'ab')",
    "{k: 1 for k in 'ab'}",
    "f'{ok}'",
    "f'{__import__(\"os\")}'",
    "(y := 1)",
    "open('/etc/passwd')",
    "eval('1')",
    "exec('1')",
    "print(1)",
    "x[0]",
    "'abc'[0]",
    "{'a': 1}",
    "1 + 1",
    "-1 < touches",
    "'a' * 5",
    "x if ok else 1",
    "ok is None",
    "await x",
    "b'bytes' == x",
    "1j == x",
    "...",
    "[__import__('os')]",
    "x; y",
    "import os",
    "__builtins__",
    "_private == 1",
    "x.__dict__",
    "",
    "   ",
    "((((((((((((((((((((((((((((((((((((ok))))))))))))))))))))))))))))))))))))" + " and ok" * 200,
    "not " * 40 + "ok",
]


@pytest.mark.parametrize("src", ATTACKS)
def test_attack_does_not_compile(src):
    with pytest.raises(ExpressionError):
        compile_expr(src, FIELDS | {"__builtins__", "_private"})


def test_attacks_do_not_run_even_if_a_name_is_whitelisted_by_mistake():
    # a double-underscore name is refused even if the caller whitelists it
    with pytest.raises(ExpressionError):
        compile_expr("__builtins__", {"__builtins__"})


def test_compile_and_evaluate_never_touch_python_eval_or_exec(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("builtin eval/exec called")

    monkeypatch.setattr(builtins, "eval", boom)
    monkeypatch.setattr(builtins, "exec", boom)
    assert ev("kyc.status == 'APPROVED'", {"kyc.status": "APPROVED"}) is True


def test_module_source_does_not_call_eval_or_exec():
    src = (Path(__file__).resolve().parent.parent / "core/engine/safe_expr.py").read_text()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert not re.search(r"(?<![\w.])(eval|exec)\s*\(", code)
    assert "compile(" not in code.replace("compile_expr(", "")


def test_overlong_source_is_refused():
    with pytest.raises(ExpressionError):
        compile_expr("ok == 1 " + "or ok == 1 " * 100, FIELDS)
