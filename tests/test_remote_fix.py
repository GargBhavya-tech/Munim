"""#93 remote-fix toolkit: restart, volume reset, config push with rollback (R2), connectivity check; firmware is ops-only and is never an
agent tool; a remote fix runs BEFORE any replacement (the replacement precondition needs a remote attempt)."""
import json

import pytest

from core.actions.types import ActionIntent
from core.engine.scripted import scripted_decider
from packs.paytm_merchant.actions import build_registry
from tests.test_scenarios_s1_c2 import SCRIPT, mid_args
from tests.world import World

MID = "M-0005"


@pytest.fixture
def w(db_path, clock):
    w = World(db_path, clock, None, mid=MID)
    w.engine.decider = scripted_decider(SCRIPT)
    return w


def fault(w, kind):
    w.control("/mock/control/device-fault/{mid}", status=kind)


def submit(w, action, args=None, seq=1, case="CS-R"):
    return w.executor.submit(ActionIntent(action, {"merchant_id": MID, **(args or {})}, case_id=case, step_seq=seq, allowlist=(action,)))


def device(w):
    return w.reader("get_device_status", {"merchant_id": MID})


# ---- the Watch out: firmware for the agent --------------------------------------------------------------------------------------------
def test_no_firmware_action_exists_for_the_agent():
    reg = build_registry()
    assert not [n for n in reg.names() if "firmware" in n]
    agent_names = {n for n in reg.names() if reg.get(n).agent_callable}
    assert {"restart_device", "reset_device_volume", "push_device_config", "check_device_connectivity"} <= agent_names
    assert "rollback_device_config" not in agent_names                      # the compensation is never offered to the model


# ---- each tool fixes only its own fault ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fault_kind,action,args,fixes", [
    ("NEEDS_RESTART", "restart_device", {}, True),
    ("VOLUME_ZERO", "reset_device_volume", {}, True),
    ("CONFIG_STALE", "push_device_config", {"config_version": "cfg-9"}, True),
    ("HARDWARE", "restart_device", {}, False),
    ("NETWORK", "restart_device", {}, False),
    ("NEEDS_RESTART", "reset_device_volume", {}, False),
])
def test_a_remote_tool_fixes_only_the_fault_it_is_for(w, fault_kind, action, args, fixes):
    fault(w, fault_kind)
    assert device(w)["last_test_announcement_ok"] in (0, False)
    out = submit(w, action, args)
    assert out.status == "OK", out.error
    assert out.result["result"] == ("FIXED" if fixes else "NO_CHANGE") and out.result["test_announcement_ok"] is fixes
    d = device(w)
    assert (d["status"] == "ONLINE" and bool(d["last_test_announcement_ok"])) is fixes
    assert d["remote_fix_attempts"] == 1                                    # a failed attempt still counts: all remote steps used up


def test_connectivity_is_a_read_that_reports_weak_signal_and_does_not_count_as_a_fix(w):
    fault(w, "NETWORK")
    c = w.reader("check_device_connectivity", {"merchant_id": MID})
    assert c["weak_signal"] is True and c["signal_pct"] == 12 and c["reachable"] is False
    assert device(w)["remote_fix_attempts"] == 0


def test_a_config_push_is_rolled_back_within_its_window_and_not_after(w):
    fault(w, "CONFIG_STALE")
    out = submit(w, "push_device_config", {"config_version": "cfg-9"})
    assert out.status == "OK"
    undo = w.executor.undo(out.action_id, __import__("core.actions.types", fromlist=["Principal"]).Principal("HUMAN", "u-1", "support_executive"))
    assert undo.status == "OK"
    assert w.conn.execute("SELECT status FROM device_remote_ops WHERE op = 'config_push'").fetchone()[0] == "ROLLED_BACK"
    again = submit(w, "push_device_config", {"config_version": "cfg-10"}, seq=2)
    w.clock.advance(__import__("datetime").timedelta(hours=25))
    late = w.executor.undo(again.action_id, __import__("core.actions.types", fromlist=["Principal"]).Principal("HUMAN", "u-1", "support_executive"))
    assert late.status == "REFUSED" and "WINDOW_CLOSED" in late.error


def test_repeating_the_same_remote_call_does_not_repeat_the_fix(w):
    fault(w, "NEEDS_RESTART")
    a, b = submit(w, "restart_device", seq=1), submit(w, "restart_device", seq=1)       # same case, same step: the same idempotency key
    assert (a.status, b.status) == ("OK", "REPLAYED") and a.result == b.result
    assert w.conn.execute("SELECT COUNT(*) FROM device_remote_ops WHERE op = 'restart'").fetchone()[0] == 1


# ---- Done when: remote fixes run before dispatch (a replacement) -----------------------------------------------------------------------
def run_case(w, fault_kind):
    fault(w, fault_kind)
    w.inject("H-1", "DEVICE_HEARTBEAT_LOST"); w.dispatcher.run()
    w.inject("H-2", "MERCHANT_MESSAGE", payload={"text": "sab check kiya, band hai"}); w.dispatcher.run()
    return w.case("C2_SOUNDBOX_FAULT")


def ops(w):
    return [r[0] for r in w.conn.execute("SELECT op FROM device_remote_ops ORDER BY rowid")]


def replacements(w):
    return w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE merchant_id = ? AND status = 'CREATED' AND created_at >= '2026-10-31'", (MID,)).fetchone()[0]


def test_a_remotely_fixable_fault_never_gets_a_replacement(w):
    case = run_case(w, "NEEDS_RESTART")
    assert ops(w) == ["restart"] and replacements(w) == 0
    assert case.state == "RESOLVED"                                         # online and the test announcement passed: done by telemetry, not trust


def test_a_hardware_fault_tries_the_remote_fix_first_then_replaces_once(w):
    case = run_case(w, "HARDWARE")
    assert ops(w) == ["restart"] and replacements(w) == 1
    seq = [json.loads(r[0])["action"] for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'ACTION_RESULT' ORDER BY seq")
           if json.loads(r[0]).get("status") == "OK" and json.loads(r[0])["action"] in ("restart_device", "create_device_replacement")]
    assert seq == ["restart_device", "create_device_replacement"]


def test_a_replacement_is_refused_until_a_remote_attempt_exists(w):
    fault(w, "HARDWARE")
    w.engine.decider = scripted_decider({("C2_SOUNDBOX_FAULT", "check_basics"): ("create_device_replacement", mid_args(reason="NO_ANNOUNCEMENT")),
                                         ("C2_SOUNDBOX_FAULT", "remote_fix"): ("create_device_replacement", mid_args(reason="NO_ANNOUNCEMENT"))})
    w.inject("H-1", "DEVICE_HEARTBEAT_LOST"); w.dispatcher.run()
    w.inject("H-2", "MERCHANT_MESSAGE", payload={"text": "band hai"}); w.dispatcher.run()
    assert replacements(w) == 0
    refusals = [json.loads(r[0]) for r in w.conn.execute("SELECT payload_json FROM ledger WHERE kind = 'POLICY'")]
    assert any(p["action"] == "create_device_replacement" and "PRECONDITION" in (p.get("reason") or "") for p in refusals)
