"""Integration: the engine, the real handoff service and the ledger agree on exactly one ESCALATION entry per escalation."""
import json

from core.audit.ledger import verify_chain
from core.audit.replay import replay_case
from core.engine.handoff import HandoffService
from core.eval.scenario import load_scenario
from tests.checkpoint_script import SCRIPT
from tests.scenario_runner import check, run_scenario
from tests.test_scenario_runner import SC_DIR
from tests.world import World


def with_handoffs(world):
    svc = HandoffService(world.conn, world.clock, world.ledger, executor=world.executor, on_return=world.engine.handle_event)
    world.engine.on_escalate = lambda case, reasons, violations: svc.create_packet(case, reasons, violations)
    return svc


def test_one_escalation_entry_one_handoff_and_replay_counts_it_once(db_path, clock):
    sc = load_scenario(SC_DIR / "dev" / "S1_ACTIVATION__escalate_name_mismatch.yaml")
    w = World(db_path, clock, SCRIPT, mid=sc.persona.merchant_id)
    svc = with_handoffs(w)
    assert check(sc, run_scenario(w, sc)) == []
    case = w.case()
    rows = w.conn.execute("SELECT payload_json FROM ledger WHERE kind='ESCALATION' AND case_id=?", (case.case_id,)).fetchall()
    assert len(rows) == 1, "exactly one ESCALATION entry per escalation"
    payload = json.loads(rows[0][0])
    assert payload["handoff_id"].startswith("HO-") and payload["reasons"]
    h = svc.for_case(case.case_id)
    assert h and h[0].status == "OPEN" and h[0].packet["case_id"] == case.case_id
    r = replay_case(w.conn, case.case_id)
    assert len(r.state["escalations"]) == 1 and r.state["status"] == "ESCALATED"
    assert verify_chain(w.conn).ok


def test_without_a_service_the_engine_still_writes_its_own_single_entry(db_path, clock):
    sc = load_scenario(SC_DIR / "dev" / "S1_ACTIVATION__escalate_name_mismatch.yaml")
    w = World(db_path, clock, SCRIPT, mid=sc.persona.merchant_id)
    run_scenario(w, sc)
    n = w.conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='ESCALATION' AND case_id=?", (w.case().case_id,)).fetchone()[0]
    assert n == 1
