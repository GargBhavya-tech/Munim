"""Run a Scenario against a World and compare the FINAL STATE of the mock systems with `expected`.

Test infrastructure shared by every playbook's scenario tests. It judges outcomes by system state and the audit ledger,
never by reading a transcript. The case is driven by a scripted decider (no LLM), so a scenario proves the playbook,
policy and executor behave; it does not measure model quality (that is the evaluation phase).
"""
import json
from dataclasses import dataclass, field
from datetime import timedelta

from core.eval.scenario import Scenario
from core.engine.fields import SYSTEM_FIELDS, build_namespace
from core.engine.intents import top_intent

OPEN = ("NEW", "ACTIVE", "WAITING", "PARKED", "NOT_NOW", "BLOCKED", "ESCALATED", "RETURNED_TO_AGENT")


@dataclass
class Result:
    case_state: str | None
    escalated: bool
    done_fields: dict
    executed: dict = field(default_factory=dict)   # action -> times it executed (status OK)
    refused: dict = field(default_factory=dict)    # action -> times policy/engine refused it
    touches: int = 0
    ledger_ok: bool = True


def _case(world, sc):
    row = world.conn.execute("SELECT case_id FROM cases WHERE merchant_id=? AND playbook_id=? ORDER BY opened_at DESC, case_id DESC",
                             (sc.persona.merchant_id, sc.playbook_id)).fetchone()
    return world.engine.repo.load(row[0]) if row else None


def run_scenario(world, sc: Scenario) -> Result:
    world.mid = mid = sc.persona.merchant_id
    for n, st in enumerate(sc.steps):
        if st.after_hours:
            world.clock.advance(timedelta(hours=st.after_hours))
        eid = f"{sc.id}-{n}"
        if st.event:
            world.inject(eid, st.event)
        elif st.merchant_says is not None:
            case = _case(world, sc)
            if case is None or case.state not in OPEN:
                world.engine.open_case(mid, sc.playbook_id, language=sc.persona.language)
            world.inject(eid, "MERCHANT_MESSAGE", payload={"text": st.merchant_says, "intent": top_intent(st.merchant_says, sc.persona.language)})
        elif st.control:
            world.control(st.control["path"], **st.control.get("body", {}))
        elif st.pay is not None:
            world.pay(f"{eid}-pay", st.pay)
        elif st.advance_settlement:
            row = world.conn.execute("SELECT settlement_id FROM settlements WHERE merchant_id=? AND status IN ('PENDING','HELD_RISK') ORDER BY expected_at DESC LIMIT 1", (mid,)).fetchone()
            assert row, f"{sc.id}: no settlement to advance at step {n}"
            world.advance_settlement(row[0], f"{eid}-adv", to=st.advance_settlement)
        elif st.chaos:
            assert world.http.post("/mock/chaos", json=st.chaos).status_code == 200
        world.dispatcher.run()
    case = _case(world, sc)
    reads = sorted(set(SYSTEM_FIELDS.values()))
    ns = build_namespace(world.reader, mid, reads, {})
    executed, refused = {}, {}
    for r in world.conn.execute("SELECT kind, payload_json FROM ledger WHERE kind IN ('ACTION_RESULT', 'POLICY') ORDER BY seq"):
        p = json.loads(r["payload_json"])
        if r["kind"] == "ACTION_RESULT" and p.get("status") == "OK" and p.get("cls", "R3") != "R0":
            executed[p["action"]] = executed.get(p["action"], 0) + 1
        elif r["kind"] == "POLICY" and p.get("allow") is False:
            refused[p["action"]] = refused.get(p["action"], 0) + 1
    from core.audit.ledger import verify_chain
    escalated = bool(world.conn.execute("SELECT 1 FROM ledger WHERE kind='ESCALATION' AND case_id=?", (case.case_id,)).fetchone()) if case else False
    return Result(case.state if case else None, escalated, {f: ns.get(f) for f in sc.expected.done_fields}, executed, refused, case.touches if case else 0, verify_chain(world.conn).ok)


def check(sc: Scenario, res: Result, read_only_actions: frozenset = frozenset()) -> list[str]:
    """Problems found when comparing the result with the scenario's expectations. Empty list = the scenario passed."""
    e, bad = sc.expected, []
    if res.case_state != e.case_state:
        bad.append(f"case_state {res.case_state} != {e.case_state}")
    for f, want in e.done_fields.items():
        if res.done_fields.get(f) != want:
            bad.append(f"{f} is {res.done_fields.get(f)!r}, expected {want!r}")
    if res.escalated != e.escalation.expected:
        bad.append(f"escalated={res.escalated}, expected {e.escalation.expected}")
    for a in e.forbidden_actions:
        if res.executed.get(a):
            bad.append(f"FORBIDDEN action executed: {a} x{res.executed[a]}")
    for a in e.required_actions:
        if res.executed.get(a) != 1:
            bad.append(f"required action {a} executed {res.executed.get(a, 0)} times, expected exactly once")
    if res.touches > e.max_touches:
        bad.append(f"touches {res.touches} > max {e.max_touches}")
    if not res.ledger_ok:
        bad.append("ledger chain broken")
    return bad
