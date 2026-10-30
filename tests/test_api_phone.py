"""HTTP contract of /api/judge/phone/* (#130) and a full phone flow: redeem -> message -> scoreboard."""
import pytest

from core.api import routes_phone
from tests.test_judge_mode import say, setup  # noqa: F401  (setup is a fixture)
from tests.test_turn_assembly import STATUS, decision, render


@pytest.fixture
def phone_api(setup):
    def make():
        api, stack, prov = setup(phone_secret="s3cret")
        api.app.include_router(routes_phone.router)
        return api, stack, prov
    return make


def issue(api, persona="P-1", **kw):
    r = api.post("/api/judge/phone/tokens", json={"persona_id": persona, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_issue_returns_token_and_qr_path(phone_api):
    api, _, _ = phone_api()
    b = issue(api, "P-2")
    assert b["token"].startswith("jt_") and b["path"] == f"/phone?token={b['token']}" and b["persona_id"] == "P-2" and b["label"] == "SIMULATED"


def test_issue_503_without_secret_and_404_unknown_persona(setup):
    api, _, _ = setup()
    api.app.include_router(routes_phone.router)
    assert api.post("/api/judge/phone/tokens", json={"persona_id": "P-1"}).status_code == 503
    api.app.state.phone_secret = "x"
    assert api.post("/api/judge/phone/tokens", json={"persona_id": "P-NOPE"}).status_code == 404
    assert api.post("/api/judge/phone/tokens", json={"persona_id": "P-1", "ttl_s": 1}).status_code == 422


def test_redeem_once_then_409_and_status_codes(phone_api):
    api, _, _ = phone_api()
    t = issue(api, "P-1")["token"]
    r = api.post("/api/judge/phone/redeem", json={"token": t})
    assert r.status_code == 200 and r.json()["session"]["persona_id"] == "P-1" and r.json()["scoreboard"]["label"] == "SIMULATED"
    assert api.post("/api/judge/phone/redeem", json={"token": t}).json()["detail"] == {"error": "TOKEN_USED"}
    assert api.post("/api/judge/phone/redeem", json={"token": t}).status_code == 409
    assert api.post("/api/judge/phone/redeem", json={"token": "jt_bad"}).status_code == 404
    t2 = issue(api, "P-1")["token"]
    assert api.post("/api/judge/phone/redeem", json={"token": t2, "persona_id": "P-2"}).status_code == 403


def test_expired_is_410(phone_api):
    from datetime import timedelta
    api, stack, _ = phone_api()
    t = issue(api, ttl_s=30)["token"]
    stack.clock.advance(timedelta(seconds=31))
    assert api.post("/api/judge/phone/redeem", json={"token": t}).status_code == 410


def test_a_judge_drives_a_case_from_the_phone_session(phone_api):
    api, stack, prov = phone_api()
    t = issue(api, "P-1")["token"]
    sid = api.post("/api/judge/phone/redeem", json={"token": t}).json()["session"]["session_id"]
    body = say(api, prov, sid, "my settlement has not come", [decision(None, [STATUS]), render(STATUS)], language="en-IN")
    assert body["scoreboard"]["attempts"] == 1 and body["replies"][0]["text"].endswith("Settlements status is PENDING.")


def test_the_demo_stack_can_issue_a_phone_token_without_setup(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.api.dev_server import build
    monkeypatch.delenv("MUNIM_PHONE_SECRET", raising=False)
    app, _ = build(tmp_path / "demo.db", demo=False)
    r = TestClient(app).post("/api/judge/phone/tokens", json={"persona_id": "P-4", "ttl_s": 600})
    assert r.status_code == 200 and r.json()["path"].startswith("/phone?token=")


def test_the_live_stack_turns_on_break_munim_and_the_guard_off_twin(tmp_path, capsys):
    """enable_live_extras with fake models: both signature features answer instead of 404 (they were off on the dev server even with --llm)."""
    from fastapi.testclient import TestClient
    from core.api.dev_server import attach_llm, build, enable_live_extras
    from core.engine.knowledge_layer import KnowledgeLayer
    from tests.fakes_gate import FakeNLI, FakeTranslator, llm

    class NoHits:
        def search(self, query, scope, k):
            return []

    def attach(stack):
        attach_llm(stack, client=llm([], stack.ledger)[0], nli=FakeNLI(), translator=FakeTranslator(), knowledge=KnowledgeLayer(NoHits()))
    app, stack = build(tmp_path / "demo.db", demo=False)
    http = TestClient(app)
    assert http.get("/api/arena").status_code == 404 and http.get("/api/twin").status_code == 404      # the scripted dev stack: off
    enable_live_extras(app, tmp_path / "demo.db", stack.clock, tmp_path / "live", attach_sandbox=attach, client_factory=lambda ledger: llm([], ledger)[0])
    arena = http.get("/api/arena").json()
    assert len(arena["levels"]) == 7 and arena["label"] == "SIMULATED"
    assert http.get("/api/twin").status_code == 200
    assert "Arena admin token" in capsys.readouterr().out
