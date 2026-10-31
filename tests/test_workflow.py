"""Engine loop, state machine and crash safety (#15). Collaborators are small doubles defined here."""
import hashlib
import json
from dataclasses import dataclass, field

import pytest

from core.actions.registry import ActionRegistry
from core.actions.types import ActionIntent, ActionOutcome, Violation
from core.audit.ledger import Ledger, verify_chain
from core.audit.replay import replay_case
from core.engine.cases import CaseRepo, IllegalTransition
from core.engine.scripted import scripted_decider
from core.engine.timers import TimerService
from core.engine.workflow import Decision, Engine
from packs.paytm_merchant.actions import build_registry

MID = "M-0001"


class CrashNow(BaseException):
    """Simulated process death: not an Exception, so nothing in the engine can swallow it."""


@dataclass
class Step:
    name: str


@dataclass
class PB:
    id: str
    priority_lane: str
    steps: list
    allowed_actions: list = field(default_factory=list)
    done_check: str = ""
    max_touches: int = 3


class World:
    """The 'systems' the diagnose double reads: a step is done once the world says so."""
    def __init__(self):
        self.done: set[str] = set()


def make_diagnose(world: World):
    def diagnose(playbook, reader, merchant_id, facts):
        for s in playbook.steps:
            if s not in world.done:
                return Step(s)
        return None
    return diagnose


def make_done(world: World):
    return lambda pb, reader, mid, facts: all(s in world.done for s in pb.steps)


class FakeExecutor:
    """Honours idempotency keys like the real one: same case|action|args|step_seq => same outcome, executed once."""
    def __init__(self, world: World | None = None, ledger: Ledger | None = None):
        self.world = world
        self.ledger = ledger
        self.calls: list[ActionIntent] = []
        self.executed: list[ActionIntent] = []
        self.results: dict[str, ActionOutcome] = {}
        self.completes = True          # an executed action completes its step in the world
        self.refuse: set[str] = set()
        self.escalate: set[str] = set()
        self.intent_logged_first: list[bool] = []

    def key(self, i: ActionIntent) -> str:
        raw = f"{i.case_id}|{i.action}|{json.dumps(i.args, sort_keys=True)}|{i.step_seq}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def submit(self, intent: ActionIntent) -> ActionOutcome:
        self.calls.append(intent)
        if self.ledger is not None:
            rows = [e for e in self.ledger.entries(intent.case_id) if e.kind == "ACTION_INTENT" and e.payload["step_seq"] == intent.step_seq]
            self.intent_logged_first.append(bool(rows))
        k = self.key(intent)
        if k in self.results:
            prev = self.results[k]
            return ActionOutcome("REPLAYED", k, result=prev.result)
        if intent.action in self.refuse:
            out = ActionOutcome("REFUSED", k, violations=[Violation("TEST-001", "t", "BLOCK", "no", action=intent.action)])
        elif intent.action in self.escalate:
            out = ActionOutcome("ESCALATE", k, violations=[Violation("TEST-ESC", "t", "ESCALATE", "human")])
        else:
            self.executed.append(intent)
            if self.world is not None and self.completes and intent.step:
                self.world.done.add(intent.step)
            out = ActionOutcome("OK", k, result={"ok": True})
        self.results[k] = out
        return out

    def undo(self, action_id, principal):  # pragma: no cover
        raise NotImplementedError


class CrashingExecutor:
    """Wraps an executor and kills the 'process' once, before or after the real call."""
    def __init__(self, inner, when="before"):
        self.inner, self.when, self.armed = inner, when, True

    def submit(self, intent):
        if self.armed and self.when == "before":
            self.armed = False
            raise CrashNow()
        out = self.inner.submit(intent)
        if self.armed and self.when == "after":
            self.armed = False
            raise CrashNow()
        return out


S1 = PB("S1", "P2", ["kyc", "bank"], ["send_kyc_resume_link", "retrigger_bank_verification"])
TABLE = {("S1", "kyc"): ("send_kyc_resume_link", {"merchant_id": MID}),
         ("S1", "bank"): ("retrigger_bank_verification", {"merchant_id": MID})}


def build(conn, clock, *, playbooks=None, executor=None, world=None, decider=None, hard=None, with_timers=False, **kw):
    world = world or World()
    ledger = Ledger(conn, clock)
    ex = executor or FakeExecutor(world, ledger)
    timers = TimerService(conn, clock, ledger) if with_timers else None
    pbs = playbooks or {"S1": S1}
    eng = Engine(conn, clock, ledger, build_registry(), pbs, lambda n, a: {}, ex, make_diagnose(world),
                 decider or scripted_decider(TABLE), hard_escalation=hard, timers=timers, done_fn=make_done(world), **kw)
    return eng, ex, world, ledger, timers


def ev(eid, type_="SIGNUP_STALLED", case=None, mid=MID, **payload):
    return {"event_id": eid, "type": type_, "merchant_id": mid, "case_id": case, "payload": payload}


def kinds(ledger, case_id):
    return [e.kind for e in ledger.entries(case_id)]


# ---------------------------------------------------------------- state machine
def test_illegal_transitions_raise(conn, clock):
    ledger = Ledger(conn, clock)
    repo = CaseRepo(conn, clock, ledger)
    c = repo.open_case(playbook_id="S1", lane="P2", merchant_id=MID)
    assert c.case_id == "CS-0001" and c.state == "NEW"
    with pytest.raises(IllegalTransition):
        repo.transition(c, "RESOLVED")          # NEW cannot jump to RESOLVED
    c = repo.transition(c, "ACTIVE")
    c = repo.transition(c, "RESOLVED")
    for bad in ("ACTIVE", "WAITING", "ESCALATED", "PARKED"):
        with pytest.raises(IllegalTransition):
            repo.transition(c, bad)             # RESOLVED is terminal
    assert repo.load(c.case_id).state == "RESOLVED"
    # the failed attempts left no STATE entry behind
    assert [e.payload["state"] for e in ledger.entries(c.case_id)] == ["NEW", "ACTIVE", "RESOLVED"]


def test_escalated_cannot_resume_without_human_return(conn, clock):
    repo = CaseRepo(conn, clock, Ledger(conn, clock))
    c = repo.transition(repo.open_case(playbook_id="S1", lane="P2", merchant_id=MID), "ACTIVE")
    c = repo.transition(c, "ESCALATED")
    with pytest.raises(IllegalTransition):
        repo.transition(c, "ACTIVE")
    c = repo.transition(c, "RETURNED_TO_AGENT")
    assert repo.transition(c, "ACTIVE").state == "ACTIVE"


# ---------------------------------------------------------------- happy path + replay
def test_s1_runs_to_resolved_and_replay_agrees(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    case = eng.open_case(MID, "S1")
    r = eng.handle_event(ev("E-1", case=case.case_id))
    assert r.state == "RESOLVED"
    assert [i.action for i in ex.executed] == ["send_kyc_resume_link", "retrigger_bank_verification"]
    assert [i.step_seq for i in ex.executed] == [1, 2]
    assert all(i.lane == "P2" and i.playbook_id == "S1" for i in ex.executed)
    rep = replay_case(conn, case.case_id)
    assert rep.chain.ok and rep.state["status"] == "RESOLVED"
    assert [a["action"] for a in rep.state["actions"]] == ["send_kyc_resume_link", "retrigger_bank_verification"]
    assert [a["status"] for a in rep.state["actions"]] == ["OK", "OK"]
    assert rep.state["events"] == ["SIGNUP_STALLED"]
    built = CaseRepo(conn, clock, ledger).rebuild(case.case_id)
    assert built["status"] == "RESOLVED" == CaseRepo(conn, clock, ledger).load(case.case_id).state
    assert conn.execute("SELECT closed_at FROM cases WHERE case_id=?", (case.case_id,)).fetchone()[0] is not None
    assert verify_chain(conn).ok


def test_ledger_entry_precedes_every_side_effect(conn, clock):
    """Watch-out: ledger written AFTER the side effect. The fake checks the ACTION_INTENT row exists at call time."""
    eng, ex, *_ = build(conn, clock)
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", case=case.case_id))
    assert len(ex.calls) == 2 and ex.intent_logged_first == [True, True]


def test_state_and_event_entries_come_before_action_entries(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", case=case.case_id))
    ks = kinds(ledger, case.case_id)
    assert ks.index("EVENT") < ks.index("ACTION_INTENT") < ks.index("ACTION_RESULT")
    states = [e.payload["state"] for e in ledger.entries(case.case_id) if e.kind == "STATE"]
    assert states[:2] == ["NEW", "ACTIVE"] and states[-1] == "RESOLVED"


def test_event_dedupe_one_transition(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    case = eng.open_case(MID, "S1")
    e = ev("E-1", case=case.case_id)
    assert eng.handle_event(e).status == "PROCESSED"
    n_ledger = len(ledger.entries(case.case_id))
    assert eng.handle_event(e).status == "DUPLICATE"
    assert len(ledger.entries(case.case_id)) == n_ledger
    assert kinds(ledger, case.case_id).count("EVENT") == 1
    assert [s for s in kinds(ledger, case.case_id)].count("ACTION_INTENT") == 2
    assert len(ex.executed) == 2
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event_id='E-1'").fetchone()[0] == 1


def test_event_dedupe_does_not_apply_a_second_state_change(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock, executor=None)
    ex.completes = False  # step never completes: case ends WAITING
    case = eng.open_case(MID, "S1")
    e = ev("E-1", case=case.case_id)
    eng.handle_event(e)
    before = [x.payload["state"] for x in ledger.entries(case.case_id) if x.kind == "STATE"]
    eng.handle_event(e)
    assert [x.payload["state"] for x in ledger.entries(case.case_id) if x.kind == "STATE"] == before


# ---------------------------------------------------------------- crash safety
def test_crash_after_ledger_write_before_executor_runs_action_exactly_once(conn, clock):
    world = World()
    ledger = Ledger(conn, clock)
    inner = FakeExecutor(world, ledger)
    eng, ex, _, _, _ = build(conn, clock, executor=CrashingExecutor(inner, "before"), world=world)
    case = eng.open_case(MID, "S1")
    with pytest.raises(CrashNow):
        eng.handle_event(ev("E-1", case=case.case_id))
    # the process died: the intent is on the ledger, the action was NOT performed
    assert kinds(ledger, case.case_id).count("ACTION_INTENT") == 1 and kinds(ledger, case.case_id).count("ACTION_RESULT") == 0
    assert inner.executed == []
    # restart: brand-new engine over the same database and the same (surviving) remote system
    eng2, _, _, _, _ = build(conn, clock, executor=inner, world=world)
    assert eng2.recover() == ["E-1"]
    assert [i.action for i in inner.executed] == ["send_kyc_resume_link", "retrigger_bank_verification"]  # each exactly once
    assert len(inner.calls) == 2
    assert eng2.repo.load(case.case_id).state == "RESOLVED"
    assert kinds(ledger, case.case_id).count("ACTION_INTENT") == 2   # nothing planned twice
    assert eng2.recover() == []                                       # and a second restart does nothing
    assert len(inner.executed) == 2
    assert replay_case(conn, case.case_id).state["status"] == "RESOLVED"


def test_crash_after_executor_before_result_logged_does_not_duplicate(conn, clock):
    world = World()
    ledger = Ledger(conn, clock)
    inner = FakeExecutor(world, ledger)
    eng, *_ = build(conn, clock, executor=CrashingExecutor(inner, "after"), world=world)
    case = eng.open_case(MID, "S1")
    with pytest.raises(CrashNow):
        eng.handle_event(ev("E-1", case=case.case_id))
    assert len(inner.executed) == 1 and kinds(ledger, case.case_id).count("ACTION_RESULT") == 0
    eng2, *_ = build(conn, clock, executor=inner, world=world)
    eng2.recover()
    assert [i.action for i in inner.executed] == ["send_kyc_resume_link", "retrigger_bank_verification"]
    first = [e.payload for e in ledger.entries(case.case_id) if e.kind == "ACTION_RESULT"][0]
    assert first["status"] == "REPLAYED"   # the second submit hit the idempotency key
    assert replay_case(conn, case.case_id).state["status"] == "RESOLVED"


def test_crash_between_event_and_decision_resumes(conn, clock):
    """Crash after the EVENT entry but before anything else: recover() picks the event up."""
    world = World()
    boom = {"on": True}
    inner_dec = scripted_decider(TABLE)

    def decider(ctx):
        if boom["on"]:
            boom["on"] = False
            raise CrashNow()
        return inner_dec(ctx)
    eng, ex, _, ledger, _ = build(conn, clock, world=world, decider=decider)
    case = eng.open_case(MID, "S1")
    with pytest.raises(CrashNow):
        eng.handle_event(ev("E-1", case=case.case_id))
    assert kinds(ledger, case.case_id).count("EVENT") == 1 and ex.calls == []
    eng.recover()
    assert len(ex.executed) == 2 and eng.repo.load(case.case_id).state == "RESOLVED"
    assert kinds(ledger, case.case_id).count("EVENT") == 1


# ---------------------------------------------------------------- reactions to outcomes
def test_refused_outcome_blocks_the_case_and_stops(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    ex.refuse = {"send_kyc_resume_link"}
    case = eng.open_case(MID, "S1")
    r = eng.handle_event(ev("E-1", case=case.case_id))
    assert r.state == "BLOCKED"
    st = [e for e in ledger.entries(case.case_id) if e.kind == "STATE"][-1]
    assert st.payload["state"] == "BLOCKED" and st.payload["step"] == "kyc" and "TEST-001" in st.payload["reason"]
    assert [e.payload["status"] for e in ledger.entries(case.case_id) if e.kind == "ACTION_RESULT"] == ["REFUSED"]
    assert len(ex.calls) == 1


def test_escalate_outcome_escalates(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    ex.escalate = {"send_kyc_resume_link"}
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", case=case.case_id)).state == "ESCALATED"
    assert [e.payload for e in ledger.entries(case.case_id) if e.kind == "ESCALATION"] == [{"reasons": ["TEST-ESC"]}]
    assert replay_case(conn, case.case_id).state["status"] == "ESCALATED"


def test_hard_escalation_runs_before_the_decider_and_the_executor(conn, clock):
    calls = []

    def decider(ctx):
        calls.append(ctx)
        return Decision()
    eng, ex, world, ledger, _ = build(conn, clock, decider=decider, hard=lambda case, e: ["REGULATOR_MENTION"] if e.type == "MERCHANT_MESSAGE" else [])
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", "MERCHANT_MESSAGE", case=case.case_id)).state == "ESCALATED"
    assert calls == [] and ex.calls == []
    assert [e.payload["reasons"] for e in ledger.entries(case.case_id) if e.kind == "ESCALATION"] == [["REGULATOR_MENTION"]]


def test_human_return_resumes_an_escalated_case(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock, hard=lambda c, e: ["X"] if e.type == "MERCHANT_MESSAGE" else [])
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", "MERCHANT_MESSAGE", case=case.case_id))
    assert eng.handle_event(ev("E-2", "MERCHANT_MESSAGE", case=case.case_id)).state == "ESCALATED"  # still human-owned
    assert ex.calls == []
    assert eng.handle_event(ev("E-3", "HUMAN_RETURNED", case=case.case_id)).state == "RESOLVED"
    assert len(ex.executed) == 2


def test_closes_resolved_only_when_done_check_passes(conn, clock):
    world = World()
    world.done = {"kyc", "bank"}
    eng, ex, _, _, _ = build(conn, clock, world=world)
    # diagnose says "nothing left" but the systems' done_check says no -> must not close
    eng.done_fn = lambda *a: False
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", case=case.case_id)).state == "WAITING"
    eng.done_fn = make_done(world)
    assert eng.handle_event(ev("E-2", case=case.case_id)).state == "RESOLVED"


def test_re_diagnoses_after_each_step_and_does_not_repeat_a_step_in_one_event(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    ex.completes = False                      # the action does not fix the step (merchant must act first)
    case = eng.open_case(MID, "S1")
    r = eng.handle_event(ev("E-1", case=case.case_id))
    assert r.state == "WAITING" and len(ex.executed) == 1     # not hammered in a loop
    world.done.add("kyc")                                     # merchant completed KYC; a new event arrives
    r = eng.handle_event(ev("E-2", "KYC_SUBMITTED", case=case.case_id))
    assert [i.action for i in ex.executed] == ["send_kyc_resume_link", "retrigger_bank_verification"]
    assert [i.step_seq for i in ex.executed] == [1, 2]       # step_seq keeps counting across events


def test_scripted_decider_without_entry_waits(conn, clock):
    eng, ex, *_ = build(conn, clock, decider=scripted_decider({}))
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", case=case.case_id)).state == "WAITING"
    assert ex.calls == []


def test_event_for_closed_case_is_logged_and_ignored(conn, clock):
    eng, ex, world, ledger, _ = build(conn, clock)
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", case=case.case_id))
    n = len(ex.calls)
    assert eng.handle_event(ev("E-2", case=case.case_id)).status == "IGNORED"
    assert len(ex.calls) == n and eng.repo.load(case.case_id).state == "RESOLVED"


def test_follow_up_timer_is_scheduled_on_wait_and_cancelled_on_close(conn, clock):
    from datetime import timedelta
    world = World()
    table = {("S1", "kyc"): {"action": "send_kyc_resume_link", "args": {"merchant_id": MID}, "follow_up_hours": 24}}
    eng, ex, _, ledger, timers = build(conn, clock, world=world, decider=scripted_decider(table), with_timers=True)
    ex.completes = False
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", case=case.case_id)).state == "WAITING"
    pend = timers.pending(case.case_id)
    assert len(pend) == 1 and pend[0]["kind"] == "follow_up"
    # 24h later the timer fires into the engine; the step is not done, nothing is re-sent in this event? (new event => new action)
    world.done.update({"kyc", "bank"})
    clock.advance(timedelta(hours=25))
    assert eng.repo.load(case.case_id).state == "RESOLVED"
    assert timers.pending(case.case_id) == []


# ---------------------------------------------------------------- found live (C1): a refused reply left the merchant with silence
HANDOFF_EN = "I am passing this to a colleague who will help you."


class RefusesModelText(FakeExecutor):
    """The policy engine refuses the model's message (as LOAN-RATE-001 did live) but not the fixed hand-over line."""
    def submit(self, intent):
        if intent.action == "send_message" and intent.args.get("body") != HANDOFF_EN:
            self.calls.append(intent)
            return ActionOutcome("REFUSED", self.key(intent), violations=[Violation("LOAN-RATE-001", "t", "BLOCK", "rate in text", action="send_message")])
        return super().submit(intent)


def test_a_refused_reply_sends_the_fixed_hand_over_line_and_a_person_takes_the_case(conn, clock):
    ex = RefusesModelText()
    eng, _, _, ledger, _ = build(conn, clock, executor=ex, decider=lambda ctx: Decision(message_en="Your loan is approved at 0%.", language="en-IN"),
                                 refused_message_line=lambda language: HANDOFF_EN)
    ex.ledger = ledger
    case = eng.open_case(MID, "S1")
    r = eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="give me a 0% loan"))
    sent = [e.payload["text"] for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"]
    assert sent == [HANDOFF_EN]                                                  # the merchant hears the approved line, never the refused text
    assert r.state == "ESCALATED"
    esc = [e for e in ledger.entries(case.case_id) if e.kind == "ESCALATION"][-1]
    assert any("LOAN-RATE-001" in x for x in esc.payload["reasons"])


def test_without_the_hook_a_refused_reply_still_blocks(conn, clock):
    ex = RefusesModelText()
    eng, _, _, ledger, _ = build(conn, clock, executor=ex, decider=lambda ctx: Decision(message_en="Your loan is approved at 0%.", language="en-IN"))
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="give me a 0% loan")).state == "BLOCKED"
    assert [e for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == []


# ---------------------------------------------------------------- found live (C2): an escalation on a merchant's message left them with silence
def test_an_escalation_on_a_merchants_message_tells_them_a_person_is_taking_over(conn, clock):
    """A risk hold escalated at once (hard trigger) and the merchant who asked 'where is my settlement?' got no answer at all."""
    eng, ex, _, ledger, _ = build(conn, clock, hard=lambda case, ev: ["settlement.status == 'HELD_RISK'"], refused_message_line=lambda language: HANDOFF_EN)
    case = eng.open_case(MID, "S1")
    r = eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="where is my settlement?", channel="app"))
    assert r.state == "ESCALATED"
    assert [e.payload["text"] for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == [HANDOFF_EN]
    assert [i.args["channel"] for i in ex.executed if i.action == "send_message"] == ["app"]      # the channel the merchant used


def test_a_system_event_escalation_sends_nothing(conn, clock):
    """Nobody asked: an escalation from a system event (a hold detected overnight) is a dashboard matter, not a message."""
    eng, _, _, ledger, _ = build(conn, clock, hard=lambda case, ev: ["settlement.status == 'HELD_RISK'"], refused_message_line=lambda language: HANDOFF_EN)
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", type_="SETTLEMENT_HELD", case=case.case_id)).state == "ESCALATED"
    assert [e for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == []


def test_an_action_the_playbook_rejects_on_a_merchants_message_goes_to_a_person_with_the_line(conn, clock):
    """Live: a replacement over the playbook limit was rejected and the Kannada merchant heard nothing."""
    pb = PB("S1", "P2", ["kyc", "bank"], ["retrigger_bank_verification"])                        # send_kyc_resume_link is NOT allowed
    eng, _, _, ledger, _ = build(conn, clock, playbooks={"S1": pb}, refused_message_line=lambda language: HANDOFF_EN)
    case = eng.open_case(MID, "S1")
    r = eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="help"))
    assert r.state == "ESCALATED"
    assert [e.payload["text"] for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == [HANDOFF_EN]


def test_no_hand_over_line_when_the_merchant_was_already_answered(conn, clock):
    eng, _, _, ledger, _ = build(conn, clock, decider=lambda ctx: Decision(message_en="Your settlement is pending.", escalate=["WANTS_HUMAN"]),
                                 refused_message_line=lambda language: HANDOFF_EN)
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="I want a person"))
    assert [e.payload["text"] for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == ["Your settlement is pending."]


# ---------------------------------------------------------------- found in the 31 Oct rehearsal: silence AFTER the first hand-over
def test_a_merchant_who_writes_again_while_a_person_has_the_case_is_told_so_not_left_in_silence(conn, clock):
    """Rehearsal: the first message escalated and got the hand-over line; every later message on the escalated case was logged and answered with nothing."""
    eng, _, _, ledger, _ = build(conn, clock, hard=lambda case, ev: ["settlement.status == 'HELD_RISK'"], refused_message_line=lambda language: HANDOFF_EN)
    case = eng.open_case(MID, "S1")
    assert eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="where is my settlement?", channel="app")).state == "ESCALATED"
    r = eng.handle_event(ev("E-2", type_="MERCHANT_MESSAGE", case=case.case_id, text="hello? is anyone there?", channel="app"))
    assert r.state == "ESCALATED"                                                                            # still a person's case
    assert [e.payload["text"] for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == [HANDOFF_EN, HANDOFF_EN]
    assert len([e for e in ledger.entries(case.case_id) if e.kind == "ESCALATION"]) == 1                      # told again, not escalated again


def test_a_system_event_on_an_escalated_case_still_sends_nothing(conn, clock):
    eng, _, _, ledger, _ = build(conn, clock, hard=lambda case, ev: ["settlement.status == 'HELD_RISK'"], refused_message_line=lambda language: HANDOFF_EN)
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="where is my settlement?", channel="app"))
    eng.handle_event(ev("E-2", type_="SETTLEMENT_HELD", case=case.case_id))
    assert [e.payload["text"] for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == [HANDOFF_EN]


def test_without_the_hook_a_follow_up_on_an_escalated_case_stays_silent(conn, clock):
    eng, _, _, ledger, _ = build(conn, clock, hard=lambda case, ev: ["settlement.status == 'HELD_RISK'"])
    case = eng.open_case(MID, "S1")
    eng.handle_event(ev("E-1", type_="MERCHANT_MESSAGE", case=case.case_id, text="where is my settlement?"))
    eng.handle_event(ev("E-2", type_="MERCHANT_MESSAGE", case=case.case_id, text="hello?"))
    assert [e for e in ledger.entries(case.case_id) if e.kind == "MESSAGE_SENT"] == []
