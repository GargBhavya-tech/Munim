"""HTTP contract of /api/arena/* (#132). The twin's HTTP contract is in tests/test_judge_twin.py (banner on every response)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import routes_arena
from core.judge.arena import Arena, ArenaConfig
from tests.test_judge_arena import lang_en, make, say  # noqa: F401  (fixtures)
from tests.test_turn_assembly import STATUS, decision, render


@pytest.fixture
def client(make):
    a = make(player_burst=99, global_burst=99)
    app = FastAPI()
    app.state.arena = a
    app.state.arena_admin_token = "s3cret"
    app.include_router(routes_arena.router)
    return TestClient(app), a


def test_disabled_without_an_arena():
    app = FastAPI()
    app.include_router(routes_arena.router)
    r = TestClient(app).get("/api/arena")
    assert r.status_code == 404 and r.json()["detail"]["error"] == "ARENA_DISABLED"


def test_info_lists_seven_levels_and_labels_simulated(client):
    c, a = client
    b = c.get("/api/arena").json()
    assert b["label"] == "SIMULATED" and b["state"] == "RUNNING" and len(b["levels"]) == 7 and "sql" not in b["levels"][0]
    assert b["budget"] == {"spent_paise": 0, "budget_paise": 50_000}


def test_join_validates_nickname(client):
    c, a = client
    assert c.post("/api/arena/players", json={"nickname": "ab"}).status_code == 400
    assert c.post("/api/arena/players", json={"nickname": "Paytm Admin"}).json()["detail"]["error"] == "NICKNAME_REJECTED"
    ok = c.post("/api/arena/players", json={"nickname": "Asha"})
    assert ok.status_code == 200 and ok.json()["player_id"] == "AP-0001" and len(ok.json()["token"]) == 24
    assert c.post("/api/arena/players", json={"nickname": "asha"}).status_code == 409


def test_attempt_flow_and_unknown_token(client):
    c, a = client
    tok = c.post("/api/arena/players", json={"nickname": "Asha"}).json()["token"]
    a.sandbox_prov("AP-0001").replies[:] = [decision(None, [STATUS]), render(STATUS)]
    r = c.post(f"/api/arena/players/{tok}/attempts", json={"text": "my settlement has not come", "language": "en-IN"})
    assert r.status_code == 200 and r.json()["attempt_no"] == 1 and r.json()["cleared_now"] == [] and r.json()["replies"][0]["text"].endswith("PENDING.")
    assert c.get(f"/api/arena/players/{tok}").json()["attempts"] == 1
    assert c.post("/api/arena/players/bad/attempts", json={"text": "hi"}).status_code == 404
    assert c.post(f"/api/arena/players/{tok}/attempts", json={"text": ""}).status_code == 422


def test_kill_switch_needs_the_admin_token(client):
    c, a = client
    tok = c.post("/api/arena/players", json={"nickname": "Asha"}).json()["token"]
    assert c.post("/api/arena/admin/kill").status_code == 403
    assert c.post("/api/arena/admin/kill", headers={"X-Arena-Admin": "wrong"}).status_code == 403
    assert c.post("/api/arena/admin/kill", headers={"X-Arena-Admin": "s3cret"}).json()["state"] == "KILLED"
    r = c.post(f"/api/arena/players/{tok}/attempts", json={"text": "hi"})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "ARENA_STOPPED"
    assert c.post("/api/arena/admin/revive", headers={"X-Arena-Admin": "s3cret"}).json()["state"] == "RUNNING"


def test_admin_is_closed_when_no_token_is_configured(make):
    app = FastAPI()
    app.state.arena = make()
    app.include_router(routes_arena.router)
    assert TestClient(app).post("/api/arena/admin/kill", headers={"X-Arena-Admin": ""}).status_code == 403


def test_rate_limit_is_429(make):
    a = make(player_burst=1)
    app = FastAPI()
    app.state.arena = a
    app.include_router(routes_arena.router)
    c = TestClient(app)
    tok = c.post("/api/arena/players", json={"nickname": "Asha"}).json()["token"]
    a.sandbox_prov("AP-0001").replies[:] = [decision(None, [STATUS]), render(STATUS)]
    assert c.post(f"/api/arena/players/{tok}/attempts", json={"text": "my settlement has not come"}).status_code == 200
    r = c.post(f"/api/arena/players/{tok}/attempts", json={"text": "again"})
    assert r.status_code == 429 and r.json()["detail"]["cap"] == "player_burst"
