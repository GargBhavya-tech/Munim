"""GET /api/judge/personas: the Judge Mode screen lists the seeded persona cards from here."""
from fastapi.testclient import TestClient

from core.api.app import create_api
from tests.checkpoint_script import SCRIPT
from tests.world import World


def test_personas_are_listed_in_id_order_with_their_merchant_and_a_simulated_label(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    body = TestClient(create_api(w.db_path, w.clock, w.executor, w.http)).get("/api/judge/personas").json()
    ps = body["personas"]
    assert body["label"] == "SIMULATED" and [p["persona_id"] for p in ps] == sorted(p["persona_id"] for p in ps) and len(ps) == 6
    assert ps[0] == {"persona_id": "P-1", "title": ps[0]["title"], "merchant_id": "M-0001", "description": ps[0]["description"]}
    assert all(p["merchant_id"].startswith("M-") and p["title"] for p in ps)
