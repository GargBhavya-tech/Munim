"""#133 soundbox prop: payment announcement within 1 s SIM time, no duplicate after reconnect, brief verified, brief never acts without 'haan'."""
from datetime import timedelta

import pytest

from core.gate.faithfulness import FaithfulnessChecker
from core.gate.phrases import Phrases
from core.gate.pipeline import GroundingGate
from core.gate.render import Renderer
from core.gate.sources import Claim, SourceBook
from core.gate.verify import GroundingVerifier
from core.voice.readback import ReadBackGate
from prop.brief import MorningBrief, Proposal
from prop.client import SoundboxProp
from tests.checkpoint_script import SCRIPT
from tests.fakes_gate import FakeNLI, FakeTranslator, llm
from tests.world import World

M = "M-0001"


def cl(text, *ids):
    return {"claim_ids": list(ids), "text": text}


def pay(eid, seq, paise=45_000):
    return {"event_id": eid, "seq": seq, "type": "PAYMENT_SUCCESS", "merchant_id": M, "payload": {"amount_paise": paise}, "ts_sim": "x"}


class Transport:
    """Scripted channel: each poll answers a list of events or raises. Replays everything after `cursor` unless `replay_all`."""
    def __init__(self, clock, script, latency=timedelta(milliseconds=200)):
        self.clock, self.script, self.latency, self.calls = clock, list(script), latency, []

    def poll(self, cursor):
        self.calls.append((self.clock.now(), cursor))
        r = self.script.pop(0) if self.script else []
        if isinstance(r, Exception):
            raise r
        self.clock.advance(self.latency)
        return r


def make(world, replies, script, **kw):
    client, _ = llm(replies)
    nli = FakeNLI()
    ph = Phrases()
    gate = GroundingGate(GroundingVerifier(nli), Renderer(client, ph), FaithfulnessChecker(nli, FakeTranslator()), ledger=world.ledger)
    spoken = []
    rb = ReadBackGate(world.conn, world.clock, world.registry, ledger=world.ledger)
    submitted = []
    prop = SoundboxProp(M, Transport(world.clock, script), gate, world.clock, tts=lambda t, l: (t.encode(), 1.0), play=spoken.append,
                        language="en-IN", readback=rb, transcribe=lambda wav: wav.decode(), submit=lambda a, g: submitted.append((a, g)), **kw)
    return prop, spoken, submitted, gate, rb


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


PAY_REPLY = {"sentences": [cl("The payment event amount is 450 rupees.", "pay")]}


def test_payment_is_announced_within_one_second_of_sim_time_with_the_system_amount(world):
    prop, spoken, _, _, _ = make(world, [PAY_REPLY], [[pay("E1", 1)]])
    assert prop.tick() == 1
    a = prop.announcements[0]
    assert "450 rupees" in a.text and spoken == [a.text.encode()]
    assert 0 <= a.latency_s <= 1.0


def test_the_amount_comes_from_the_event_not_the_model_an_invented_amount_is_not_spoken(world):
    wrong = {"sentences": [cl("The payment event amount is 999 rupees.", "pay")]}
    prop, spoken, _, _, _ = make(world, [wrong, wrong], [[pay("E1", 1)]])
    assert prop.tick() == 0 and spoken == [] and prop.silenced and prop.silenced[0][0] == "E1"


def test_flaky_link_backs_off_in_sim_time_then_recovers_without_duplicate_announcements(world):
    script = [[pay("E1", 1)], ConnectionError("wifi"), ConnectionError("wifi"), [pay("E1", 1), pay("E2", 2, 10_000)]]
    prop, spoken, _, _, _ = make(world, [PAY_REPLY, {"sentences": [cl("The payment event amount is 100 rupees.", "pay")]}], script)
    assert prop.tick() == 1
    assert prop.tick() == 0                       # fails; waits 1 s
    n = len(prop.transport.calls)
    assert prop.tick() == 0 and len(prop.transport.calls) == n         # still backing off: no poll
    world.clock.advance(timedelta(seconds=1))
    assert prop.tick() == 0                       # fails again; waits 2 s
    world.clock.advance(timedelta(seconds=1))
    assert prop.tick() == 0 and len(prop.transport.calls) == n + 1     # 1 s is not enough now
    world.clock.advance(timedelta(seconds=1))
    assert prop.tick() == 1                       # E1 replayed (ignored), E2 announced
    assert [a.event_id for a in prop.announcements] == ["E1", "E2"] and len(spoken) == 2
    assert prop.reconnects == 2


def test_other_event_types_and_other_merchants_are_not_announced(world):
    other = pay("E9", 3)
    other["merchant_id"] = "M-0002"
    fail = {**pay("E8", 2), "type": "PAYMENT_FAILED"}
    prop, spoken, _, _, _ = make(world, [], [[other, fail]])
    assert prop.tick() == 0 and spoken == []


# ------------------------------------------------------------------------------------------------ brief
TOOL = "tool:get_settlements#b1"


def brief_inputs():
    b = SourceBook()
    b.add_tool_result(TOOL, {"amount_paise": 820000, "status": "PENDING"})
    return [Claim("c1", "The amount is 8200 rupees.", TOOL), Claim("c2", "The status is PENDING.", TOOL)], b


GOOD = {"sentences": [cl("The amount is 8200 rupees.", "c1"), cl("The status is PENDING.", "c2")]}
WAIVER = Proposal("apply_fee_waiver", {"merchant_id": M, "months": 1, "offer_id": "OF-1"})


def test_brief_sentences_are_all_verified_and_an_unsupported_claim_is_dropped(world):
    prop, spoken, _, gate, rb = make(world, [GOOD], [])
    claims, book = brief_inputs()
    claims.append(Claim("c3", "The fee is 99 rupees.", TOOL))                 # invented number
    br = MorningBrief(gate, rb).build(M, claims, book, language="en-IN", case_id="C-b")
    assert br.text == "The amount is 8200 rupees. The status is PENDING."
    assert "99" not in br.text and {v.claim_id for v in br.outcome.blocked} == {"c3"}


def test_a_brief_with_nothing_verified_is_not_spoken_and_proposes_nothing(world):
    prop, spoken, _, gate, rb = make(world, [], [])
    _, book = brief_inputs()
    br = MorningBrief(gate, rb).build(M, [Claim("c3", "The fee is 99 rupees.", TOOL)], book, language="en-IN", proposals=[WAIVER], case_id="C-b")
    assert br.text is None and br.holds == [] and prop.say_brief(br) is False and spoken == []
    assert rb.pending("C-b", M) is False


def _briefed(world, answer=None):
    prop, spoken, submitted, gate, rb = make(world, [GOOD], [], case_id="C-b")
    claims, book = brief_inputs()
    br = MorningBrief(gate, rb).build(M, claims, book, language="en-IN", proposals=[WAIVER], case_id="C-b")
    prop.say_brief(br)
    if answer is not None:
        prop.push_to_talk(answer.encode())
    return prop, spoken, submitted, rb


def test_the_brief_never_acts_without_haan(world):
    prop, spoken, submitted, rb = _briefed(world)
    assert "Is that right?" in spoken[-1].decode() and rb.pending("C-b", M)    # proposal read back, held
    assert submitted == []
    world.clock.advance(timedelta(hours=2))                                    # silence / timeout
    assert prop.push_to_talk(b"haan").status == "NONE" and submitted == []


@pytest.mark.parametrize("answer", ["nahi", "pata nahi kya", "haan 3000", "bolo", ""])
def test_no_unclear_or_different_number_answers_never_act(world, answer):
    _, _, submitted, _ = _briefed(world, answer)
    assert submitted == []


def test_haan_confirms_exactly_once_and_hands_the_action_to_the_caller(world):
    prop, _, submitted, rb = _briefed(world, "haan")
    assert submitted == [("apply_fee_waiver", WAIVER.args)]
    prop.push_to_talk(b"haan")
    assert len(submitted) == 1


def test_speech_without_a_pending_read_back_is_not_an_action(world):
    prop, _, submitted, _, _ = make(world, [], [])
    assert prop.push_to_talk(b"haan karo waiver").status == "NONE" and submitted == []
