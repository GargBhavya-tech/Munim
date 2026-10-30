"""#75: offer bands come ONLY from the pricing system (get_offer_band). Never from a playbook file, a prompt or a script."""
import re
from pathlib import Path

import pytest

from core.actions.compensation import SYSTEM as UNDO
from core.actions.types import AGENT, ActionIntent, Principal
from core.eval.scenario import load_scenario
from tests.scenario_runner import check, run_scenario
from tests.test_scenarios_sales import SC_DIR, sales_world
from tests.world import PB_DIR, World

KIRANA, D2C = "M-0007", "M-0009"
SALES_LEAD = Principal("HUMAN", "sl-1", "sales_lead")
AREA_MANAGER = Principal("HUMAN", "am-1", "area_manager")
MONTH_COUNT = re.compile(r"(\d+|one|two|three|four|six|twelve)[\s-]*(months?|mahine?)\b|\bmonths\s*[:=]\s*\d|max_months|\bband\s*[:=]\s*\d", re.I)


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, {})


_seq = iter(range(1, 10_000))


def waiver(world, mid, months, principal=AGENT, allow=True):
    n = next(_seq)
    intent = ActionIntent("apply_fee_waiver", {"merchant_id": mid, "months": months}, case_id=f"CS-P{n}", step_seq=n, principal=principal,
                          allowlist=("apply_fee_waiver",) if principal.kind == "AGENT" and allow else None)
    return world.executor.submit(intent)


def set_band(world, segment, months):
    world.conn.execute("UPDATE offer_bands SET max_months=? WHERE segment=?", (months, segment))


# ---------------------------------------------------------------------------------- the band is read from the pricing system
def test_the_band_differs_by_segment_and_comes_from_the_tool(world):
    assert world.reader("get_offer_band", {"merchant_id": KIRANA})["max_months"] == 1
    assert world.reader("get_offer_band", {"merchant_id": D2C})["max_months"] == 2


def test_changing_the_band_in_the_pricing_system_changes_the_waiver_the_agent_may_give(world):
    assert waiver(world, KIRANA, 1).status == "OK"
    out = waiver(world, KIRANA, 2)
    assert out.status == "ESCALATE" and out.violations[0].rule_id == "WAIVER-BAND-001"
    set_band(world, "kirana", 3)                                                     # the pricing team widens the band
    assert waiver(world, KIRANA, 3).status == "OK"
    assert waiver(world, KIRANA, 4).status == "ESCALATE"
    set_band(world, "kirana", 0)                                                     # ... and closes it
    assert waiver(world, KIRANA, 1).status == "ESCALATE"
    assert world.conn.execute("SELECT COUNT(*) FROM offers WHERE merchant_id=?", (KIRANA,)).fetchone()[0] == 2     # only the two in-band ones were applied


def test_only_the_changed_segment_moves(world):
    set_band(world, "kirana", 4)
    assert waiver(world, D2C, 2).status == "OK" and waiver(world, D2C, 3).status == "ESCALATE"      # d2c is still 2


def test_a_scenario_rerun_with_a_wider_band_flips_the_outcome(db_path, clock, tmp_path):
    """The S2 six-month ask escalates at band 1 and is applied when the pricing system says 6: no playbook or script was touched."""
    sc = load_scenario(next((SC_DIR / "dev").glob("S2_SOUNDBOX_UPSELL__should_escalate_six_month_waiver.yaml")))
    w = sales_world(db_path, clock, sc.playbook_id, sc.persona.merchant_id)
    assert check(sc, run_scenario(w, sc)) == []                                       # band 1: escalated

    from core.memory.store import copy_db
    from data.seed import seed
    from core.engine.clock import SimClock
    db2 = tmp_path / "band6.db"
    seed(db2, 42)
    w2 = sales_world(db2, SimClock(), sc.playbook_id, sc.persona.merchant_id)
    set_band(w2, "kirana", 6)
    wide = sc.model_copy(deep=True)
    wide.expected.case_state, wide.expected.escalation.expected, wide.expected.escalation.reason_class = "WAITING", False, None
    wide.expected.forbidden_actions, wide.expected.required_actions = ["create_device_order"], ["apply_fee_waiver"]
    wide.expected.done_fields = {"offer.latest_status": "APPLIED"}
    assert check(wide, run_scenario(w2, wide)) == []


def test_if_the_band_cannot_be_read_the_waiver_escalates_instead_of_being_applied(world):
    world.http.post("/mock/chaos", json={"system": "pricing", "mode": "error500", "rate": 1.0})
    out = waiver(world, KIRANA, 1)
    assert out.status == "ESCALATE" and out.violations[0].rule_id == "WAIVER-BAND-001"
    assert world.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_an_agent_never_gets_more_than_the_band_whatever_the_intent_carries(world):
    out = world.executor.submit(ActionIntent("apply_fee_waiver", {"merchant_id": KIRANA, "months": 6}, case_id="CS-X", step_seq=1, principal=AGENT,
                                             allowlist=("apply_fee_waiver",), facts={"band": 99, "max_months": 99, "offer_band": 99}))
    assert out.status == "ESCALATE"                                                  # facts claiming a wide band are ignored


def test_out_of_band_goes_to_the_sales_lead_and_a_sales_lead_is_capped(world):
    out = waiver(world, KIRANA, 6)
    assert out.status == "ESCALATE" and out.violations[0].route == "sales_lead"
    assert waiver(world, KIRANA, 3, principal=SALES_LEAD).status == "OK"             # the sales lead's cap (limits.yaml) is 3
    over = waiver(world, KIRANA, 4, principal=SALES_LEAD)
    assert over.status in ("REFUSED", "ESCALATE") and {v.rule_id for v in over.violations} & {"ROLE-001", "WAIVER-BAND-001"}
    assert waiver(world, KIRANA, 2, principal=AREA_MANAGER).status == "OK"


def test_a_waiver_in_band_is_revocable_inside_its_window_only(world):
    out = waiver(world, D2C, 2)
    assert out.status == "OK" and out.compensable_until is not None
    assert world.executor.undo(out.action_id, UNDO).status == "OK"      # the merchant's Undo button; an agent cannot undo itself
    assert world.conn.execute("SELECT status FROM offers WHERE merchant_id=?", (D2C,)).fetchone()[0] == "REVOKED"
    late = waiver(world, D2C, 1)
    world.clock.advance(__import__("datetime").timedelta(days=8))
    assert world.executor.undo(late.action_id, UNDO).status == "REFUSED"


# ---------------------------------------------------------------------------------------------- no band in any file
def test_no_playbook_file_contains_a_month_count_or_a_band():
    assert MONTH_COUNT.search("waive 3 months of rental") and MONTH_COUNT.search("months: 2") and MONTH_COUNT.search("max_months")   # the lint can fail
    offenders = {p.name: m.group(0) for p in PB_DIR.glob("*.yaml") for m in [MONTH_COUNT.search(p.read_text(encoding="utf-8"))] if m}
    assert offenders == {}


def test_sales_playbooks_that_can_waive_read_the_band_and_carry_no_constraint_on_months(world):
    for pid in ("S2_SOUNDBOX_UPSELL", "S4_PLAN_CHANGE"):
        pb = world.playbooks[pid]
        assert "get_offer_band" in pb.required_reads
        a = pb.action("apply_fee_waiver")
        assert a is not None and a.constraints == {}
    for pid in ("S3_LOAN", "S5_WINBACK"):
        assert world.playbooks[pid].action("apply_fee_waiver") is None            # a loan or a win-back never carries a fee waiver


def test_the_playbook_loader_source_has_no_band_table():
    root = Path(__file__).resolve().parent.parent
    assert "offer_bands" not in (root / "packs/paytm_merchant/policy/limits.yaml").read_text()      # the only place numbers live is the pricing DB
