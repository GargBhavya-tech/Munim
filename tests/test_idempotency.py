"""#24: idempotency keys are a pure, stable function of the logical action."""
import hashlib
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

from core.actions.idempotency import IdempotencyStore, make_key, normalise_args

ROOT = Path(__file__).resolve().parent.parent


def expected(case, action, args_json, seq):
    return hashlib.sha256(f"{case}|{action}|{args_json}|{seq}".encode("utf-8")).hexdigest()


def test_key_matches_the_specified_formula():
    k = make_key("C-1", "apply_fee_waiver", {"months": 1, "merchant_id": "M-0001"}, 3)
    assert k == expected("C-1", "apply_fee_waiver", '{"merchant_id":"M-0001","months":1}', 3)


def test_same_inputs_same_key_and_any_input_change_changes_it():
    a = make_key("C-1", "x", {"a": 1}, 0)
    assert a == make_key("C-1", "x", {"a": 1}, 0)
    assert len({a, make_key("C-2", "x", {"a": 1}, 0), make_key("C-1", "y", {"a": 1}, 0), make_key("C-1", "x", {"a": 2}, 0), make_key("C-1", "x", {"a": 1}, 1)}) == 5


def test_float_and_int_amounts_give_the_same_key():
    assert make_key("C", "create_refund_request", {"amount": 100.0}, 0) == make_key("C", "create_refund_request", {"amount": 100}, 0)
    assert make_key("C", "r", {"amount_paise": 25000.0}, 0) == make_key("C", "r", {"amount_paise": 25000}, 0)
    assert normalise_args({"amount_paise": 25000.0}) == {"amount_paise": 25000}
    assert isinstance(normalise_args({"amount_paise": 25000.0})["amount_paise"], int)
    # a money float that is not whole paise is rounded to integer paise, never kept as a float
    assert normalise_args({"amount_paise": 250.4}) == {"amount_paise": 250}


def test_key_order_and_none_values_do_not_matter():
    assert make_key("C", "a", {"x": 1, "y": 2}, 0) == make_key("C", "a", {"y": 2, "x": 1}, 0)
    assert make_key("C", "a", {"x": 1, "note": None}, 0) == make_key("C", "a", {"x": 1}, 0)
    assert make_key("C", "a", {"x": 1, "n": {"q": None, "r": 2}}, 0) == make_key("C", "a", {"x": 1, "n": {"r": 2}}, 0)


def test_case_id_none_is_a_dash():
    assert make_key(None, "a", {}, 0) == expected("-", "a", "{}", 0)


def test_booleans_are_not_turned_into_ints():
    assert make_key("C", "a", {"f": True}, 0) != make_key("C", "a", {"f": 1}, 0)


def test_kannada_is_stable_and_utf8():
    kn = "ನಮಸ್ಕಾರ ಬೆಂಗಳೂರು"
    k = make_key("C", "send_message", {"body": kn}, 0)
    assert k == expected("C", "send_message", json.dumps({"body": kn}, ensure_ascii=False, separators=(",", ":")), 0)
    assert k == make_key("C", "send_message", {"body": unicodedata.normalize("NFD", kn)}, 0)   # composed vs decomposed
    assert k != make_key("C", "send_message", {"body": "ನಮಸ್ಕಾರ"}, 0)


def test_key_is_stable_across_processes():
    code = ("from core.actions.idempotency import make_key;"
            "print(make_key('C-7','create_refund_request',{'payment_id':'P-1','amount_paise':12500.0,'merchant_id':'M-0001','n':None},4))")
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin"}).stdout.strip() for seed in ("1", "2", "random")}
    assert len(outs) == 1
    assert outs == {make_key("C-7", "create_refund_request", {"payment_id": "P-1", "amount_paise": 12500, "merchant_id": "M-0001"}, 4)}


def test_keys_are_never_random():
    """The uuid4-per-attempt mistake: calling twice must not differ."""
    assert len({make_key("C", "a", {"x": 1}, 0) for _ in range(50)}) == 1


def test_store_reserve_is_once_only(conn, clock):
    s = IdempotencyStore(conn, clock)
    assert s.reserve("k1", "ACT-1") is True
    assert s.reserve("k1", "ACT-2") is False
    assert s.get("k1")["action_id"] == "ACT-1" and s.get("k1")["result_json"] is None
    s.complete("k1", '{"ok":true}')
    assert s.get("k1")["result_json"] == '{"ok":true}'
    assert s.get("nope") is None
