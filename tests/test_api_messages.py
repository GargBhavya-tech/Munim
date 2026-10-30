"""POST /api/merchants/{mid}/messages on the dev-server stack with the production turn agent attached (attach_llm); providers are fakes."""
import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.api.dev_server import Stack, attach_llm
from core.engine.knowledge_layer import KnowledgeLayer
from tests.fakes_gate import FakeNLI, FakeTranslator, llm
from tests.test_turn_assembly import FOLLOW_UP, STATUS, decision, render


class NoHits:
    def search(self, query, scope, k):
        return []


@pytest.fixture
def setup(db_path, clock, monkeypatch):
    from core.voice import language as L
    monkeypatch.setattr(L.LanguageManager, "current", lambda self, mid, case_language=None: "en-IN")
    stack = Stack(db_path, clock)

    def talk(replies):
        client, _ = llm(replies, stack.ledger)
        attach_llm(stack, client=client, nli=FakeNLI(), translator=FakeTranslator(), knowledge=KnowledgeLayer(NoHits()))
        api = create_api(db_path, clock, stack.executor, stack.http, registry=stack.registry, after_write=stack.run)
        return TestClient(api), stack
    return talk


def test_a_typed_message_gets_only_the_gate_checked_reply_and_the_action_runs(setup):
    api, stack = setup([decision(FOLLOW_UP, [STATUS]), render(STATUS)])
    r = api.post("/api/merchants/M-0001/messages", json={"text": "my settlement has not come", "language": "en-IN"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dropped"] is False and body["event_id"].startswith("MM-")
    assert len(body["replies"]) == 1 and body["replies"][0]["text"].endswith("Settlements status is PENDING.")
    seq = body["replies"][0]["seq"]                                                          # the ledger entry /api/voice/speak/{seq} takes
    row = stack.conn.execute("SELECT kind, payload_json FROM ledger WHERE seq = ?", (seq,)).fetchone()
    assert row["kind"] == "MESSAGE_SENT" and json.loads(row["payload_json"])["text"] == body["replies"][0]["text"]
    case_id = body["replies"][0]["case_id"]
    assert stack.engine.repo.load(case_id).playbook_id == "C1_SETTLEMENT_DELAY"
    assert stack.conn.execute("SELECT COUNT(*) FROM schedules WHERE merchant_id = 'M-0001'").fetchone()[0] >= 1


def test_the_message_is_redacted_before_it_is_stored(setup):
    api, stack = setup([decision(None, [STATUS]), render(STATUS)])
    api.post("/api/merchants/M-0001/messages", json={"text": "settlement nahi aaya, mera OTP 482913 hai"})
    stored = stack.conn.execute("SELECT payload_json FROM events WHERE type = 'MERCHANT_MESSAGE' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    assert "482913" not in stored


def test_an_unknown_merchant_is_404_and_nothing_is_stored(setup):
    api, stack = setup([])
    assert api.post("/api/merchants/M-9999/messages", json={"text": "hello"}).status_code == 404
    assert stack.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'MERCHANT_MESSAGE'").fetchone()[0] == 0
