"""#36: the turn agent contract is claim-only, every claim has a source, and invalid output gets one retry then an escalation."""
import json

import pytest

from core.audit.ledger import Ledger
from core.engine.escalation import HardTriggers
from core.llm.client import LLMClient
from core.llm.decision import (INVALID_OUTPUT, InvalidDecision, TurnDecision, decide, parse_decision, response_format, to_engine_decision)
from core.llm.sarvam_http import ChatResult, SarvamError
from scripts.sarvam_spike import build_messages
from scripts.spike_data import CONTEXTS


class Scripted:
    name, model_id = "sarvam", "sarvam-105b"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, *, response_format=None, **kw):
        self.seen.append((messages, response_format))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return ChatResult(r if isinstance(r, str) else json.dumps(r), {"prompt_tokens": 3, "completion_tokens": 2}, {})


def good(**over):
    d = {"language": "hinglish", "intents": [{"intent": "SETTLEMENT_MISSING", "w": 0.9}], "focus_case": "CS-0001", "new_case_requests": [],
         "next_action": {"name": "schedule_follow_up", "args": {"merchant_id": "M-0001"}},
         "claims_en": [{"id": "c1", "text": "Settlement S-0100 of 8200 rupees is PENDING.", "source": "tool:get_settlements#call_2"}],
         "ask_clarification": False}
    d.update(over)
    return d


def claim(**over):
    c = {"id": "c1", "text": "Settlement S-0100 of 8200 rupees is PENDING.", "source": "tool:get_settlements#call_2"}
    c.update(over)
    return good(claims_en=[c])


# ---- the schema -------------------------------------------------------------------------------------------------------
def test_a_valid_decision_parses_and_converts_to_an_engine_decision_with_no_message():
    d = parse_decision(json.dumps(good()))
    assert d.language == "hinglish" and d.intents[0].w == 0.9 and d.claims_en[0].source == "tool:get_settlements#call_2"
    e = to_engine_decision(d)
    assert e.action == ("schedule_follow_up", {"merchant_id": "M-0001"}) and e.message_en is None
    assert to_engine_decision(parse_decision(json.dumps(good(next_action=None)))).action is None


@pytest.mark.parametrize("bad_source", ["", "FACTS", "memory:abc", "tool:", "tool: has space", "http://evil.example"])
def test_a_claim_without_a_proper_source_is_rejected(bad_source):
    with pytest.raises(InvalidDecision) as e:
        parse_decision(json.dumps(claim(source=bad_source)))
    assert "claims_en.0.source" in str(e.value)


def test_a_claim_with_no_source_key_is_rejected():
    c = {"id": "c1", "text": "Settlement S-0100 is PENDING."}
    with pytest.raises(InvalidDecision) as e:
        parse_decision(json.dumps(good(claims_en=[c])))
    assert "claims_en.0.source" in str(e.value)


@pytest.mark.parametrize("field", ["reply", "message_to_merchant", "reply_text", "text"])
def test_merchant_facing_text_cannot_ride_in_an_extra_field(field):
    with pytest.raises(InvalidDecision) as e:
        parse_decision(json.dumps(good(**{field: "Namaste! Aapka paisa kal aa jayega."})))
    assert field in str(e.value)


@pytest.mark.parametrize("text", [
    "Namaste! Your settlement is pending.",
    "Hello, your settlement is pending.",
    "Dear merchant, your settlement is pending.",
    "Please wait, your settlement is pending.",
    "Is your settlement pending?",
    "आपका सेटलमेंट pending है",
    "ನಿಮ್ಮ ಸೆಟಲ್‌ಮೆಂಟ್ pending",
    "Settlement is pending. " * 20,
])
def test_a_claim_that_reads_like_a_reply_is_rejected(text):
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps(claim(text=text)))


def test_duplicate_claim_ids_weights_out_of_range_and_unknown_language_are_rejected():
    two = [{"id": "c1", "text": "A is 1.", "source": "kb:d#s@v1"}, {"id": "c1", "text": "B is 2.", "source": "kb:d#s@v1"}]
    for bad in (good(claims_en=two), good(intents=[{"intent": "X", "w": 1.5}]), good(language="fr-FR"), good(language=None)):
        with pytest.raises(InvalidDecision):
            parse_decision(json.dumps(bad))


def test_language_codes_are_normalised_not_guessed():
    assert parse_decision(json.dumps(good(language="hi"))).language == "hi-IN"
    assert parse_decision(json.dumps(good(language="KN-IN"))).language == "kn-IN"
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps(good(language="kannada")))


def test_fenced_json_is_accepted_and_prose_or_arrays_are_not():
    assert parse_decision("```json\n" + json.dumps(good()) + "\n```").language == "hinglish"
    for bad in ("I think the settlement is pending.", "[1, 2]", ""):
        with pytest.raises(InvalidDecision):
            parse_decision(bad)


def test_errors_never_echo_the_offending_input():
    with pytest.raises(InvalidDecision) as e:
        parse_decision(json.dumps(claim(text="Namaste SECRET-999 ji!")))
    assert "SECRET-999" not in str(e.value)


def test_response_format_is_json_schema_with_the_contract():
    f = response_format()
    assert f["type"] == "json_schema" and f["json_schema"]["schema"]["additionalProperties"] is False
    assert set(f["json_schema"]["schema"]["properties"]) == set(TurnDecision.model_fields)


# ---- retry and escalation ---------------------------------------------------------------------------------------------
def make(replies, conn=None, clock=None):
    ledger = Ledger(conn, clock) if conn is not None else None
    prov = Scripted(replies)
    return LLMClient(prov, None, ledger, sleep=lambda s: None), prov, ledger


def test_one_invalid_reply_is_retried_once_with_the_error_but_not_the_bad_reply(conn, clock):
    client, prov, ledger = make(["BAD-REPLY-TEXT-xyz not json", good()], conn, clock)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    r = decide(client, msgs, case_id="CS-1", ledger=ledger)
    assert r.decision is not None and r.invalid_outputs == 1 and r.escalate_reason is None
    retry_msgs = prov.seen[1][0]
    assert retry_msgs[:2] == msgs and retry_msgs[2]["content"].startswith("INVALID_OUTPUT:")
    assert "BAD-REPLY-TEXT" not in json.dumps(retry_msgs)
    decisions = [e for e in ledger.entries() if e.kind == "DECISION"]
    assert [e.payload["valid"] for e in decisions] == [False, True] and all(e.model_id == "sarvam-105b" and e.prompt_hash for e in decisions)
    assert decisions[1].payload["decision"]["language"] == "hinglish"


def test_two_invalid_replies_escalate_and_the_count_trips_the_hard_trigger(conn, clock):
    client, prov, ledger = make(["nope", claim(source="")], conn, clock)
    r = decide(client, [{"role": "user", "content": "u"}], ledger=ledger)
    assert r.decision is None and r.escalate_reason == INVALID_OUTPUT and r.invalid_outputs == 2 and len(prov.seen) == 2
    triggers = HardTriggers(money_limit_paise=1_000_000).check(None, {"payload": {}}, {"invalid_output_count": r.invalid_outputs})
    assert [t.code for t in triggers] == ["INVALID_OUTPUT_TWICE"]
    one = HardTriggers(money_limit_paise=1_000_000).check(None, {"payload": {}}, {"invalid_output_count": 1})
    assert one == []


def test_no_third_attempt_is_made():
    client, prov, _ = make(["a", "b", good()])
    assert decide(client, [{"role": "user", "content": "u"}]).decision is None and len(prov.seen) == 2


def test_an_unavailable_model_is_reported_as_an_upstream_failure_not_an_exception():
    client, _, _ = make([SarvamError(500, "x"), SarvamError(500, "y")])
    r = decide(client, [{"role": "user", "content": "u"}])
    assert r.decision is None and r.outcome_error == "UPSTREAM_UNAVAILABLE: llm" and r.escalate_reason is None
    codes = [t.code for t in HardTriggers(money_limit_paise=1_000_000).check(None, {"payload": {}}, {"outcomes": [r.outcome_error]})]
    assert codes == ["TOOL_FAILURE"]


def test_the_request_asks_for_the_schema_and_the_call_is_attributed(conn, clock):
    client, prov, ledger = make([good()], conn, clock)
    decide(client, [{"role": "user", "content": "u"}], case_id="CS-7", ledger=ledger)
    assert prov.seen[0][1]["type"] == "json_schema"
    ctx = [e for e in ledger.entries() if e.kind == "CONTEXT_HASH"][0]
    assert ctx.payload["purpose"] == "decision" and ctx.case_id == "CS-7"


# ---- the 20 canned contexts (live >= 19/20 valid on Sarvam is measured by scripts/sarvam_spike.py with a key) ------------
def plausible_reply(c):
    source = next(iter(c["facts"] or c["kb"]), None)
    claims = [{"id": "c1", "text": "The tool result for this case was read.", "source": source}] if source else []
    action = {"name": c["allowed_actions"][0], "args": {}} if c["allowed_actions"] else None
    return good(language=c["language"], intents=[{"intent": "UNKNOWN", "w": 0.5}], next_action=action, claims_en=claims)


def test_the_contract_accepts_a_reply_of_the_expected_shape_for_all_20_spike_contexts():
    assert len(CONTEXTS) == 20
    for c in CONTEXTS:
        client, _, _ = make([plausible_reply(c)])
        r = decide(client, build_messages(c))
        assert r.decision is not None and r.invalid_outputs == 0, c["id"]
