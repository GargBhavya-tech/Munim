"""Guided tour of Judge Mode (#135). Real components: the dev-server Stack, the production message path, the ledger-derived scoreboard.
The LLM, entailment model and translator are the scripted stand-ins from tests/fakes_gate.py (wiring, not model quality).
Recorded fallback clips are NOT produced by this ticket's code: missing_clips() reports them honestly."""
import json
import re
from pathlib import Path

import pytest
import yaml

from core.judge import scoreboard as sb
from core.judge import session as sess
from core.judge import tour
from tests.test_judge_mode import board, say, setup, start  # noqa: F401  (setup is a fixture)
from tests.test_turn_assembly import FOLLOW_UP, INVENTED, STATUS, decision, render

ROOT = Path(__file__).resolve().parent.parent
STEPS = tour.load_tour()
IDS = ["start", "first_message", "try_attack", "unsafe_refused", "unproven_blocked", "escalation", "nothing_forbidden", "mark_fine"]


def prog(stack, sid):
    return tour.progress(stack.conn, sess.load_session(stack.conn, sid))


def done_map(p):
    return {s["id"]: s["done"] for s in p["steps"]}


# ------------------------------------------------------------------------------------------------ the data
def test_tour_has_the_eight_steps_in_order_each_with_a_fact_and_a_clip_path():
    assert [s["id"] for s in STEPS] == IDS
    for s in STEPS:
        assert s["fact"]["metric"] in tour.METRICS and s["proves"].strip() and s["instruction"].strip() and s["clip"].endswith(".webm")
    assert len({s["clip"] for s in STEPS}) == 8


def test_every_ui_element_is_a_real_testid_in_the_dashboard():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "dashboard" / "src").rglob("*.tsx") if ".test." not in p.name)
    score_keys = set(re.findall(r"key: '(\w+)'", (ROOT / "dashboard/src/judge/ScoreboardPanel.tsx").read_text(encoding="utf-8")))
    for s in STEPS:
        el = s["ui_element"]
        if el.startswith("score-"):
            assert el.removeprefix("score-") in score_keys, el
        else:
            assert f'data-testid="{el}"' in src, el
        if "ui_label" in s:
            assert s["ui_label"] in src


def test_a_step_that_names_an_unknown_metric_or_key_does_not_load(tmp_path):
    for bad in ("metric: made_up_count", "extra: 1"):
        doc = yaml.safe_load((ROOT / "core/judge/tour.yaml").read_text(encoding="utf-8"))
        if bad.startswith("metric"):
            doc["steps"][0]["fact"]["metric"] = "made_up_count"
        else:
            doc["steps"][0]["canned_state"] = {"done": True}
        p = tmp_path / "t.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(tour.TourError):
            tour.load_tour(p)



def test_missing_clips_are_reported_not_hidden(tmp_path):
    assert tour.missing_clips(tmp_path) == IDS
    (tmp_path / "tour" / "clips").mkdir(parents=True)
    (tmp_path / STEPS[2]["clip"]).write_bytes(b"x")
    assert tour.missing_clips(tmp_path) == [i for i in IDS if i != "try_attack"]


# ------------------------------------------------------------------------------------------------ live state, fresh persona (Watch out: canned state)
def test_tour_against_a_fresh_persona_shows_only_the_session_start_done(setup):
    api, stack, prov = setup()
    sid = start(api, "P-2")
    p = prog(stack, sid)
    assert done_map(p) == {**{i: False for i in IDS}, "start": True}
    assert (p["done"], p["total"], p["next"]) == (1, 8, "first_message")
    assert p["steps"][0]["evidence_seq"] == int(sid.removeprefix("JS-"))
    assert [s["evidence_seq"] for s in p["steps"][1:]] == [None] * 7
    assert next(s for s in p["steps"] if s["id"] == "nothing_forbidden")["done"] is False       # zero is only meaningful after an attempt


def test_steps_turn_done_only_when_the_ledger_shows_the_fact(setup):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    say(api, prov, sid, "my settlement has not come", [decision(FOLLOW_UP, [STATUS]), render(STATUS)])
    p = prog(stack, sid)
    assert done_map(p) == {**{i: False for i in IDS}, "start": True, "first_message": True, "nothing_forbidden": True}
    assert next(s for s in p["steps"] if s["id"] == "first_message")["evidence_seq"] == [e.seq for e in stack.ledger.entries() if e.payload.get("op") == sess.OP_ATTEMPT][0]

    # The off-topic bank ask is refused and hands the case off; a handed-off case takes no model turns until a human returns it, and only then
    # does an invented number reach the gate.
    say(api, prov, sid, "change bank", [decision(("clear_risk_hold", {}), [STATUS, {"id": "c3", "text": "Settlements fee is 99 rupees.", "source": "tool:get_settlements#r1"}]), render(STATUS)])
    hs = api.get("/api/handoffs").json()["handoffs"]
    assert hs[-1]["packet"]["reasons"] == ["TOPIC_MISMATCH: CHANGE_BANK is not answered by C1_SETTLEMENT_DELAY"]
    assert api.post(f"/api/handoffs/{hs[-1]['handoff_id']}/return", json={"instruction": "proceed", "principal_id": "u7", "role": "support_executive"}).json()["ok"]
    # The API route only records HUMAN_RETURNED (the dispatcher skips case-bound events); the engine wakes the case when it is handed that
    # event, which is what HandoffService.on_return does in production. The returned case then takes one model turn of its own.
    assert stack.conn.execute("SELECT state FROM cases WHERE case_id = ?", (hs[-1]["case_id"],)).fetchone()[0] == "ESCALATED"
    prov.replies[:] = [decision(None, [STATUS]), render(STATUS)]
    ev = stack.conn.execute("SELECT * FROM events WHERE type = 'HUMAN_RETURNED'").fetchone()
    res = stack.engine.handle_event({"event_id": ev["event_id"], "type": ev["type"], "merchant_id": ev["merchant_id"], "case_id": ev["case_id"],
                                     "payload": json.loads(ev["payload_json"])})
    assert (res.status, res.state) == ("PROCESSED", "WAITING")
    say(api, prov, sid, "how much", [decision(None, [INVENTED]), decision(None, [INVENTED])])
    b = board(api, sid)
    assert b["refused_actions"] >= 1 and b["blocked_claims"] >= 1 and b["escalations"] >= 1
    p = prog(stack, sid)
    assert done_map(p) == {**{i: True for i in IDS}, "mark_fine": False}
    assert p["next"] == "mark_fine" and p["done"] == 7
    assert all(isinstance(s["evidence_seq"], int) for s in p["steps"] if s["id"] not in ("mark_fine", "nothing_forbidden"))

    r = api.post(f"/api/judge/sessions/{sid}/feedback", json={"verdict": "fine"})
    p = prog(stack, sid)
    assert p["done"] == 8 and p["next"] is None and p["steps"][-1]["evidence_seq"] == r.json()["ledger_seq"]


def test_progress_is_per_session_one_judges_attacks_do_not_complete_anothers_tour(setup):
    api, stack, prov = setup()
    a = start(api, "P-1")
    say(api, prov, a, "how much", [decision(None, [INVENTED]), decision(None, [INVENTED])])
    b = start(api, "P-2")
    assert done_map(prog(stack, b)) == {**{i: False for i in IDS}, "start": True}
    assert prog(stack, a)["done"] > 1


def test_forbidden_executed_flips_the_invariant_step_to_not_done(setup):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    say(api, prov, sid, "my settlement has not come", [decision(FOLLOW_UP, [STATUS]), render(STATUS)])
    assert done_map(prog(stack, sid))["nothing_forbidden"] is True
    stack.ledger.append(kind="ACTION_RESULT", actor="AGENT", payload={"action": "change_bank_details", "key": "kf", "status": "OK"})
    assert done_map(prog(stack, sid))["nothing_forbidden"] is False


def test_progress_has_no_state_of_its_own_and_cannot_be_given_canned_values(setup, monkeypatch):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    n = stack.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    tables = {r[0] for r in stack.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    prog(stack, sid)
    prog(stack, sid)
    assert stack.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == n                        # read only
    assert {r[0] for r in stack.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == tables
    src = Path(tour.__file__).read_text(encoding="utf-8")
    assert "INSERT" not in src and "CREATE TABLE" not in src
    monkeypatch.setattr(sb, "compute", lambda conn, s: {"attempts": 9, "blocked_claims": 9, "refused_actions": 9, "escalations": 9, "forbidden_executed": 0, "fine_marks": 9})
    assert prog(stack, sid)["done"] == 8                                       # progress follows whatever the scoreboard (the ledger count) says ...
    monkeypatch.undo()
    assert prog(stack, sid)["done"] == 1                                       # ... and nothing is remembered
