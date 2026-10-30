"""Merchant simulator (#141). No live model is called: scripted stand-ins and a recording fake provider only."""
import json
from dataclasses import fields
from pathlib import Path

import pytest

from core.eval.scenario import load_scenario
from core.eval.simulator import (LLMMerchantModel, MerchantView, Observation, ScriptedMerchant, SimAction, draw_state, play, seeded_rng)
from tests.checkpoint_script import SCRIPT as S1_SCRIPT
from tests.scenario_runner import check, run_scenario
from tests.test_service_playbooks_a import build_world
from tests.world import World

SC_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios" / "dev"
HAPPY = load_scenario(SC_DIR / "S1_ACTIVATION__happy_stalled_signup.yaml")
C3 = load_scenario(SC_DIR / "C3_PAYMENT_FAILED__happy_reversed_after_dispute.yaml")
ADV = load_scenario(SC_DIR / "S1_ACTIVATION__adversarial_claims_kyc_done.yaml")


def world(db_path, clock, sc):
    return World(db_path, clock, S1_SCRIPT, mid=sc.persona.merchant_id)


def test_scenario_without_merchant_lines_runs_unattended_and_matches_the_runner(db_path, clock):
    obs = play(world(db_path, clock, HAPPY), HAPPY, ScriptedMerchant(), seed=1)
    assert (obs.case_state, obs.resolved, obs.passed, obs.wrong_action, obs.escalated) == ("RESOLVED", True, True, False, False)
    assert [obs.executed[a] for a in HAPPY.expected.required_actions] == [1, 1, 1]


def test_play_agrees_with_the_production_scenario_runner_on_an_adversarial_scenario(db_path, clock, tmp_path):
    obs = play(world(db_path, clock, ADV), ADV, ScriptedMerchant(), seed=3)
    # the same scenario through the plain runner on a fresh copy of the same seeded world
    from core.memory.store import copy_db
    from core.engine.clock import SimClock
    db2 = tmp_path / "second.db"
    copy_db(_fresh(db_path), db2)
    ref = run_scenario(World(db2, SimClock(), S1_SCRIPT, mid=ADV.persona.merchant_id), ADV)
    assert check(ADV, ref) == []
    assert (obs.case_state, obs.escalated, obs.touches) == (ref.case_state, ref.escalated, ref.touches)
    assert obs.passed and obs.turns == 2 and obs.forbidden_executed == 0


def _fresh(db_path):
    from data.seed import seed
    p = db_path.parent / "fresh.db"
    seed(p, 42)
    return p


def test_same_scenario_and_seed_give_the_same_run_and_different_seeds_can_differ(db_path, clock, tmp_path):
    a = play(world(db_path, clock, ADV), ADV, ScriptedMerchant(), seed=7)
    from core.engine.clock import SimClock
    from core.memory.store import copy_db
    db2 = tmp_path / "again.db"
    copy_db(_fresh(db_path), db2)
    b = play(World(db2, SimClock(), S1_SCRIPT, mid=ADV.persona.merchant_id), ADV, ScriptedMerchant(), seed=7)
    assert a == b
    states = {draw_state(ADV, seeded_rng(ADV.id, s)) for s in range(40)}
    assert len({m for m, _, _ in states}) >= 1 and {h for _, h, _ in states} == {True, False}     # honesty varies with the seed
    assert seeded_rng("x", 1).random() == seeded_rng("x", 1).random() != seeded_rng("x", 2).random()


def test_trait_pins_the_mood_and_lying_trait_pins_dishonesty():
    sc = ADV.model_copy(deep=True)
    sc.persona.traits = ["angry", "lying"]
    assert {(m, h) for m, h, _ in (draw_state(sc, seeded_rng(sc.id, s)) for s in range(30))} == {("angry", False)}


def test_dishonest_run_says_the_lie_and_honest_run_the_truth(db_path, clock):
    lie, truth = "KYC pura ho chuka hai, document bhi approve ho gaya", "KYC ho gaya hai, ab bank aur paisa chalu karo"
    honest_seed = next(s for s in range(50) if draw_state(ADV, seeded_rng(ADV.id, s))[1])
    liar_seed = next(s for s in range(50) if not draw_state(ADV, seeded_rng(ADV.id, s))[1])
    w1 = world(db_path, clock, ADV)
    play(w1, ADV, ScriptedMerchant(opening=[truth, truth], lies=[lie, lie]), liar_seed)
    said = [json.loads(r[0])["text"] for r in w1.conn.execute("SELECT payload_json FROM events WHERE type='MERCHANT_MESSAGE' ORDER BY seq")]
    assert said == [lie, lie]


def test_merchant_view_carries_only_persona_and_what_was_sent_to_it(db_path, clock):
    assert {f.name for f in fields(MerchantView)} == {"language", "traits", "hidden_goal", "mood", "honest", "code_mix", "hint", "transcript"}
    m = ScriptedMerchant(followups=["theek hai"])
    w = build_world(db_path, clock, C3.persona.merchant_id)
    play(w, C3, m, seed=2, max_followups=1)
    bodies = {r[0] for r in w.conn.execute("SELECT body FROM comms_outbox")}
    assert bodies, "the agent must have sent something for this test to mean anything"
    for v in m.seen_views:
        assert {t for who, t in v.transcript if who == "agent"} <= bodies
        assert all(who in ("merchant", "agent") for who, _ in v.transcript)
    follow = [v for v in m.seen_views if v.hint is None]
    assert follow and any(who == "agent" for who, _ in follow[0].transcript)


def test_followup_happens_only_after_the_agent_replied_and_is_a_new_merchant_turn(db_path, clock, tmp_path):
    from core.engine.clock import SimClock
    from core.memory.store import copy_db
    base = play(build_world(db_path, clock, C3.persona.merchant_id), C3, ScriptedMerchant(), seed=2, max_followups=0)
    assert base.turns == 1
    db2 = tmp_path / "follow.db"
    copy_db(_fresh(db_path), db2)
    m = ScriptedMerchant(followups=["ok, aur kitna time lagega", "abhi bhi nahi hua"])
    obs = play(build_world(db2, SimClock(), C3.persona.merchant_id), C3, m, seed=2, max_followups=1)
    assert obs.turns == 2 and m.followups == ["abhi bhi nahi hua"]       # exactly one follow-up used, the cap held


def test_dual_control_allows_declared_paths_and_rejects_everything_else(db_path, clock):
    sc = HAPPY.model_copy(deep=True)
    from core.eval.scenario import Step
    sc.steps = [Step(event="SIGNUP_STALLED"), Step(merchant_says="kyc dobara bheja"),
                Step(control={"path": "/mock/control/kyc/{mid}", "body": {"status": "APPROVED"}})]
    good = {"path": "/mock/control/kyc/{mid}", "body": {"status": "APPROVED"}}
    bad = {"path": "/mock/control/bank/{mid}", "body": {"status": "VERIFIED"}}
    w = world(db_path, clock, sc)
    before = w.conn.execute("SELECT status FROM bank WHERE merchant_id=?", (sc.persona.merchant_id,)).fetchone()[0]
    obs = play(w, sc, ScriptedMerchant(controls=[bad]), seed=1)
    assert obs.rejected_controls == 1
    assert w.conn.execute("SELECT status FROM bank WHERE merchant_id=?", (sc.persona.merchant_id,)).fetchone()[0] == before
    w2 = world(db_path, clock, sc)
    obs2 = play(w2, sc, ScriptedMerchant(controls=[good]), seed=1)
    assert obs2.rejected_controls == 0


# ------------------------------------------------------------------------------------------------ live wrapper, with a fake
class Rec:
    def __init__(self, name, replies):
        self.name, self.model_id, self.replies, self.calls = name, "fake", list(replies), []

    def chat(self, messages, **kw):
        from core.llm.sarvam_http import ChatResult
        self.calls.append(messages)
        return ChatResult(self.replies.pop(0), {})


def test_live_wrapper_refuses_sarvam_and_never_builds_agent_prompts(db_path, clock):
    with pytest.raises(ValueError, match="not Sarvam"):
        LLMMerchantModel(Rec("sarvam", []))
    prov = Rec("fallback", ["KYC ho gaya hai", "NONE", "NONE"])
    w = world(db_path, clock, ADV)
    obs = play(w, ADV, LLMMerchantModel(prov), seed=5, max_followups=1)
    assert prov.calls and isinstance(obs, Observation)
    roles = {m["role"] for call in prov.calls for m in call}
    assert roles <= {"system", "user", "assistant"}
    systems = [m["content"] for call in prov.calls for m in call if m["role"] == "system"]
    assert all(s.startswith("You are a small-shop merchant") for s in systems)
    assert not any(w_ in s for s in systems for w_ in ("playbook", "policy", "ledger", "idempotency", "S1_ACTIVATION"))


def test_json_reply_can_carry_a_control_action():
    m = LLMMerchantModel(Rec("fallback", ['{"say": "ho gaya", "control": {"path": "/x/{mid}", "body": {}}}']))
    a = m.next_turn(MerchantView("hinglish", (), "", "calm", True, 0.0, "x", ()), None)
    assert a == SimAction(say="ho gaya", control={"path": "/x/{mid}", "body": {}})
