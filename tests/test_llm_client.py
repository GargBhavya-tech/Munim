"""#34: LLM client. Every model call is attributable: the ledger says which model answered, whether it was the fallback, and what failed first."""
import json

import httpx
import pytest

from core.audit.ledger import Ledger, verify_chain
from core.llm.client import (LLMClient, LLMUnavailable, OpenAICompatProvider, SarvamProvider, build_client, load_llm_config, prompt_hash)
from core.llm.sarvam_http import ChatResult, SarvamError, SarvamHTTP, load_config

MSG = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "SECRET-MERCHANT-TEXT"}]


class Fake:
    def __init__(self, name, model_id, script):
        self.name, self.model_id, self.script, self.calls = name, model_id, list(script), []

    def chat(self, messages, *, response_format=None, **kw):
        self.calls.append((messages, response_format, kw))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(step, {"prompt_tokens": 11, "completion_tokens": 5}, {})


def make(primary, fallback=None, conn=None, clock=None, sleeps=None):
    ledger = Ledger(conn, clock) if conn is not None else None
    return LLMClient(primary, fallback, ledger, sleep=(sleeps.append if sleeps is not None else (lambda s: None))), ledger


def last_call_entry(ledger):
    return [e for e in ledger.entries() if e.kind == "CONTEXT_HASH"][-1]


def test_primary_answer_is_attributed_in_the_ledger(conn, clock):
    sar = Fake("sarvam", "sarvam-105b", ['{"ok":1}'])
    client, ledger = make(sar, None, conn, clock)
    r = client.complete(MSG, case_id="CS-1", purpose="turn", response_format={"type": "json_object"})
    assert (r.text, r.provider, r.model_id, r.on_fallback, r.tokens_in, r.tokens_out) == ('{"ok":1}', "sarvam", "sarvam-105b", False, 11, 5)
    e = last_call_entry(ledger)
    assert e.model_id == "sarvam-105b" and e.prompt_hash == prompt_hash(MSG, {"type": "json_object"}) == r.prompt_hash
    assert e.case_id == "CS-1" and e.payload["on_fallback"] is False and e.payload["tokens_in"] == 11 and e.payload["failed_attempts"] == []
    assert r.ledger_seq == e.seq and client.on_fallback is False


def test_forced_sarvam_failure_falls_back_and_the_ledger_says_so(conn, clock):
    sar = Fake("sarvam", "sarvam-105b", [SarvamError(500, "boom"), SarvamError(503, "down")])
    fb = Fake("fallback", "other-model-1", ["fallback answer"])
    sleeps = []
    client, ledger = make(sar, fb, conn, clock, sleeps)
    r = client.complete(MSG)
    assert r.text == "fallback answer" and r.provider == "fallback" and r.on_fallback is True
    e = last_call_entry(ledger)
    assert e.model_id == "other-model-1"                                   # the model that ANSWERED, not the one asked first
    assert e.payload["on_fallback"] is True and client.on_fallback is True
    assert [(a["provider"], a["error"]) for a in e.payload["failed_attempts"]] == [("sarvam", "HTTP 500"), ("sarvam", "HTTP 503")]
    assert len(sar.calls) == 2 and len(fb.calls) == 1 and sleeps == [0.5]  # one retry with backoff, then the switch
    assert client.status() == {"on_fallback": True, "last_provider": "fallback", "primary_model": "sarvam-105b", "fallback_model": "other-model-1"}


def test_a_retry_on_the_primary_is_not_a_provider_switch(conn, clock):
    sar = Fake("sarvam", "sarvam-105b", [httpx.ReadTimeout("slow"), "second try"])
    fb = Fake("fallback", "other-model-1", ["never"])
    client, ledger = make(sar, fb, conn, clock)
    r = client.complete(MSG)
    assert r.text == "second try" and r.provider == "sarvam" and fb.calls == []
    e = last_call_entry(ledger)
    assert e.model_id == "sarvam-105b" and [a["error"] for a in e.payload["failed_attempts"]] == ["ReadTimeout"]


def test_a_non_retryable_error_is_not_retried_but_still_switches_and_is_logged(conn, clock):
    sar = Fake("sarvam", "sarvam-105b", [SarvamError(401, "bad key")])
    fb = Fake("fallback", "other-model-1", ["ok"])
    client, ledger = make(sar, fb, conn, clock)
    r = client.complete(MSG)
    assert len(sar.calls) == 1 and r.provider == "fallback"
    assert last_call_entry(ledger).payload["failed_attempts"][0]["error"] == "HTTP 401"


def test_when_everything_fails_it_raises_and_the_failure_is_ledgered(conn, clock):
    sar = Fake("sarvam", "sarvam-105b", [SarvamError(500, "a"), SarvamError(500, "b")])
    fb = Fake("fallback", "other-model-1", [SarvamError(502, "c"), SarvamError(502, "d")])
    client, ledger = make(sar, fb, conn, clock)
    with pytest.raises(LLMUnavailable) as ei:
        client.complete(MSG, case_id="CS-9")
    assert [a["provider"] for a in ei.value.attempts] == ["sarvam", "sarvam", "fallback", "fallback"]
    e = last_call_entry(ledger)
    assert e.payload["ok"] is False and e.model_id is None and e.case_id == "CS-9" and len(e.payload["failed_attempts"]) == 4
    assert client.last_provider is None and client.on_fallback is False


def test_no_fallback_configured_means_a_clean_failure_not_a_hang(conn, clock):
    client, _ = make(Fake("sarvam", "sarvam-105b", [SarvamError(500, "a"), SarvamError(500, "b")]), None, conn, clock)
    with pytest.raises(LLMUnavailable):
        client.complete(MSG)


def test_the_ledger_holds_hashes_and_counts_never_prompt_text(conn, clock):
    client, ledger = make(Fake("sarvam", "sarvam-105b", ["a reply about SECRET-REPLY"]), None, conn, clock)
    client.complete(MSG)
    dump = json.dumps([e.payload for e in ledger.entries()])
    assert "SECRET-MERCHANT-TEXT" not in dump and "SECRET-REPLY" not in dump and "be brief" not in dump
    assert verify_chain(conn).ok


def test_prompt_hash_is_deterministic_and_sensitive_to_content():
    a = [{"role": "user", "content": "x", "n": 1}]
    b = [{"n": 1, "content": "x", "role": "user"}]                         # same request, different key order
    assert prompt_hash(a) == prompt_hash(b)
    assert prompt_hash(a) != prompt_hash([{"role": "user", "content": "y", "n": 1}])
    assert prompt_hash(a) != prompt_hash(a, {"type": "json_object"})


def test_overrides_and_response_format_reach_the_provider():
    sar = Fake("sarvam", "sarvam-105b", ["x"])
    LLMClient(sar).complete(MSG, response_format={"type": "json_object"}, max_tokens=50)
    assert sar.calls[0][1] == {"type": "json_object"} and sar.calls[0][2] == {"max_tokens": 50}


# ---- the real providers over a fake network ---------------------------------------------------------------------------
def test_real_providers_over_mock_transports_fall_back_with_the_right_credentials(conn, clock):
    seen = {"sarvam": [], "fallback": []}

    def sarvam_handler(req):
        seen["sarvam"].append((req.headers.get("api-subscription-key"), json.loads(req.content)))
        return httpx.Response(503, text="unavailable")

    def fallback_handler(req):
        seen["fallback"].append((req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}})

    cfg = load_config()
    sar = SarvamProvider(SarvamHTTP("sk-test", cfg, httpx.Client(transport=httpx.MockTransport(sarvam_handler), base_url=cfg["base_url"])))
    fb = OpenAICompatProvider("https://fb.example", "fb-model", "fb-key", http=httpx.Client(transport=httpx.MockTransport(fallback_handler), base_url="https://fb.example"))
    client, ledger = make(sar, fb, conn, clock)
    r = client.complete(MSG, response_format={"type": "json_object"})
    assert r.text == "hello" and r.model_id == "fb-model" and (r.tokens_in, r.tokens_out) == (7, 2)
    assert len(seen["sarvam"]) == 2 and seen["sarvam"][0][0] == "sk-test"
    assert seen["sarvam"][0][1]["model"] == cfg["chat"]["model"] and seen["sarvam"][0][1]["reasoning_effort"] is None   # reasoning off (config)
    assert seen["fallback"][0][0] == "Bearer fb-key" and "reasoning_effort" not in seen["fallback"][0][1]
    assert seen["fallback"][0][1]["response_format"] == {"type": "json_object"}
    assert last_call_entry(ledger).model_id == "fb-model"


def test_build_client_only_creates_a_fallback_when_it_is_fully_configured():
    sar_http = SarvamHTTP("k", load_config(), httpx.Client(base_url="https://api.sarvam.ai"))
    assert load_llm_config()["fallback"]["base_url"] == ""                                       # shipped empty on purpose
    assert build_client(sarvam_http=sar_http, env={}).fallback is None
    full = {"fallback": {"base_url": "https://fb.example", "model": "m"}}
    assert build_client(sarvam_http=sar_http, env={}, llm_config=full).fallback is None          # no key
    assert build_client(sarvam_http=sar_http, env={"FALLBACK_LLM_KEY": "k"}, llm_config={"fallback": {"base_url": "", "model": ""}}).fallback is None
    c = build_client(sarvam_http=sar_http, env={"FALLBACK_LLM_KEY": "k"}, llm_config=full)
    assert c.fallback.model_id == "m" and c.primary.model_id == load_config()["chat"]["model"]
