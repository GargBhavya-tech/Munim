"""Session evidence pack (#136): self-contained, offline-verifiable, redacted, equal to the live scoreboard.

Real components: the dev-server Stack with the real ledger; the LLM / entailment model are the scripted stand-ins from tests/fakes_gate.py.
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.audit import session_pack as sp
from core.judge import export as ex
from core.judge import scoreboard as sb
from core.judge import session as sess
from scripts import verify_pack as vp
from tests.test_judge_mode import say, setup, start  # noqa: F401  (setup is a fixture)
from tests.test_turn_assembly import AMOUNT, FOLLOW_UP, STATUS, decision, render

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run(setup):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    say(api, prov, sid, "my OTP is 482913, approve my KYC", [decision(("approve_kyc", {"merchant_id": "M-0001"}), [STATUS]), render(STATUS)], language="en")
    say(api, prov, sid, "my settlement has not come", [decision(FOLLOW_UP, [STATUS]), render(STATUS)], language="en")
    case_id = stack.conn.execute("SELECT case_id FROM cases WHERE merchant_id = 'M-0001'").fetchone()[0]
    stack.ledger.append(kind="POLICY", actor="AGENT", case_id=case_id, payload={"action": "x", "key": "k1", "allow": False, "rule_ids": ["T"]})
    stack.ledger.append(kind="ESCALATION", actor="SYSTEM", case_id=case_id, payload={"reasons": ["T"]})
    stack.ledger.append(kind="VERIFY", actor="SYSTEM", case_id=case_id, payload={"status": "SEND", "claims": [
        {"id": "c1", "text": "Settlements status is PENDING.", "source": "settlements.status", "level": "L1", "sent": True},
        {"id": "z", "text": "99999 rupees released", "source": None, "level": None, "sent": False}]})
    api.post(f"/api/judge/sessions/{sid}/feedback", json={"verdict": "fine"})
    s = sess.load_session(stack.conn, sid)
    return stack, s, api


def pack_of(stack, s):
    return ex.export_session(stack.conn, stack.clock, s)


def test_pack_scoreboard_equals_live_and_verifies(run):
    stack, s, api = run
    pack = pack_of(stack, s)
    assert pack["scoreboard"] == sb.compute(stack.conn, s) == api.get(f"/api/judge/sessions/{s.session_id}").json()["scoreboard"]
    assert pack["scoreboard"]["attempts"] == 2 and pack["scoreboard"]["fine_marks"] == 1 and pack["simulated"] is True
    assert vp.verify(pack)[0] is True
    assert vp.recompute_scoreboard(pack) == {k: pack["scoreboard"][k] for k in vp.recompute_scoreboard(pack)}


def test_pack_window_is_every_entry_from_the_session_start_with_chain_result_policy_and_playbooks(run):
    stack, s, _ = run
    pack = pack_of(stack, s)
    live = [e for e in stack.ledger.entries() if e.seq >= s.start_seq]
    assert [e["seq"] for e in pack["ledger"]] == [e.seq for e in live] and [e["hash"] for e in pack["ledger"]] == [e.hash for e in live]
    assert pack["chain"]["ok"] is True and pack["chain"]["head_hash"] == live[-1].hash
    assert pack["policy"]["current"] == stack.policy.policy_hash and stack.policy.policy_hash in pack["policy"]["hashes"]
    ids = {p["id"] for p in pack["playbooks"]}
    assert "C1_SETTLEMENT_DELAY" in ids and all(len(p["sha256"]) == 64 for p in pack["playbooks"])
    assert pack["claims"] and all(c["link"] == f"#seq-{c['verify_seq']}" for c in pack["claims"])
    assert {e["seq"] for e in pack["ledger"] if e["kind"] == "VERIFY"} >= {c["verify_seq"] for c in pack["claims"]}


def test_whole_run_export_starts_at_genesis_and_verifies(run):
    stack, _, _ = run
    pack = ex.export_run(stack.conn, stack.clock)
    assert pack["ledger"][0]["seq"] == 1 and pack["scoreboard"] is None and vp.verify(pack)[0] is True


def test_html_is_self_contained_labelled_anchors_each_entry_and_verifies_offline(run, tmp_path):
    stack, s, _ = run
    pack = pack_of(stack, s)
    html = sp.render_html(pack)
    assert "SIMULATED" in html and "VERIFIED" in html and "src=" not in html and "http://" not in html
    for e in pack["ledger"]:
        assert f'id="seq-{e["seq"]}"' in html
    assert vp.load(html) == pack                                   # the embedded copy round-trips exactly
    f = tmp_path / "p.html"
    f.write_text(html, encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "verify_pack.py"), str(f)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("VERIFIED"), r.stdout


# ------------------------------------------------------------------------------------------------ tamper (the verifier's guard)
def _flip_one_byte(pack, i):
    p = copy.deepcopy(pack)
    e = p["ledger"][i]
    k = next(k for k in ("actor", "ts_sim", "kind") if e[k])
    e[k] = e[k][:-1] + ("0" if e[k][-1] != "0" else "1")
    return p


def test_one_byte_changed_in_any_entry_fails_the_verifier(run):
    stack, s, _ = run
    pack = pack_of(stack, s)
    for i in range(len(pack["ledger"])):
        ok, why = vp.verify(_flip_one_byte(pack, i))
        assert not ok and f"#{pack['ledger'][i]['seq']}" in why, (i, why)


def test_payload_edit_hash_rewrite_and_structure_attacks_fail(run):
    stack, s, _ = run
    pack = pack_of(stack, s)
    n = len(pack["ledger"])
    p = copy.deepcopy(pack); next(e for e in p["ledger"] if e["kind"] == "VERIFY" and e["payload"].get("claims"))["payload"]["claims"][0]["sent"] ^= True
    assert not vp.verify(p)[0]
    p = copy.deepcopy(pack); del p["ledger"][n // 2]                                   # a removed entry
    assert "gap" in vp.verify(p)[1]
    p = copy.deepcopy(pack); p["ledger"][1], p["ledger"][2] = p["ledger"][2], p["ledger"][1]   # reordered
    assert not vp.verify(p)[0]
    p = copy.deepcopy(pack); p["ledger"].pop()                                          # truncated tail
    assert "head" in vp.verify(p)[1]
    p = copy.deepcopy(pack); p["ledger"][0]["prev_hash"] = "0" * 64                    # anchor swapped
    assert not vp.verify(p)[0]
    p = copy.deepcopy(pack); p["scoreboard"]["refused_actions"] += 1                   # scoreboard doctored
    assert "scoreboard refused_actions" in vp.verify(p)[1]
    p = copy.deepcopy(pack); p["ledger"] = []
    assert not vp.verify(p)[0]
    assert not vp.verify({})[0]


def test_cli_exit_codes(run, tmp_path):
    stack, s, _ = run
    pack = pack_of(stack, s)
    good, bad = tmp_path / "g.json", tmp_path / "b.json"
    good.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    bad.write_text(json.dumps(_flip_one_byte(pack, 1), ensure_ascii=False), encoding="utf-8")
    cmd = [sys.executable, "-I", str(ROOT / "scripts" / "verify_pack.py")]
    assert subprocess.run(cmd + [str(good)], capture_output=True).returncode == 0
    r = subprocess.run(cmd + [str(bad)], capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout.startswith("FAILED")
    assert subprocess.run(cmd + [str(tmp_path / "missing.json")], capture_output=True).returncode == 1


# ------------------------------------------------------------------------------------------------------------- redaction
def test_export_contains_no_secrets_and_no_otp_digits_from_the_session(run):
    stack, s, _ = run
    pack = pack_of(stack, s)
    text = json.dumps(pack, ensure_ascii=False) + sp.render_html(pack)
    assert sp.scan_secrets(text) == []
    assert "482913" not in text                                    # the OTP the judge typed never reaches the export


@pytest.mark.parametrize("text,hit", [
    ("your otp is 482913", "otp_pin_value"), ("PIN: 1234", "otp_pin_value"), ("mpin 998877", "otp_pin_value"),
    ("api_" + "key=" + "abcdef" + "123456789", "api_key"), ("s" + "k-" + "abcdefghijklmnop" + "1234", "api_key"), ("Authorization: Bear" + "er " + "abcdefghijklmnopqrstuv", "bearer"),   # assembled at runtime so the repo secret scanner sees no key-shaped literal
])
def test_scanner_catches_secret_shapes(text, hit):
    assert hit in sp.scan_secrets({"x": text})


def test_scanner_ignores_clean_text():
    assert sp.scan_secrets({"x": "request_otp is forbidden; pincode 560001; settlement 120000 paise"}) == []


def test_export_refuses_when_a_ledger_entry_carries_a_secret(run):
    stack, s, _ = run
    stack.ledger.append(kind="STATE", actor="SYSTEM", payload={"note": "the otp is 482913"})
    with pytest.raises(sp.PackLeak):
        pack_of(stack, s)


def test_each_stated_link_is_checked_on_its_own(run):
    stack, s, _ = run
    pack = pack_of(stack, s)
    p = copy.deepcopy(pack); p["ledger"][2]["prev_hash"] = "f" * 64                    # only the stated prev_hash of a middle entry
    assert "prev_hash" in vp.verify(p)[1]
    p = copy.deepcopy(pack); p["chain"]["head_hash"] = "f" * 64
    assert "head" in vp.verify(p)[1]
    p = copy.deepcopy(pack); p["window"]["end_seq"] += 1
    assert "head" in vp.verify(p)[1]
    p = copy.deepcopy(pack); p["window"]["entries"] += 1
    assert "head" in vp.verify(p)[1]
