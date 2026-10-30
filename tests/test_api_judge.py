"""HTTP contract of /api/judge/* (#129): shapes, status codes, the break-it card. Behaviour of the scoreboard is in tests/test_judge_mode.py."""
import pytest

from tests.test_judge_mode import board, say, setup, start  # noqa: F401  (setup is a fixture)
from tests.test_turn_assembly import STATUS, decision, render


def test_start_returns_session_scoreboard_usage_and_caps(setup):
    api, stack, prov = setup()
    r = api.post("/api/judge/sessions", json={"persona_id": "P-2"})
    assert r.status_code == 200
    b = r.json()
    assert b["session"]["persona_id"] == "P-2" and b["session"]["merchant_id"] == "M-0002" and b["session"]["session_id"].startswith("JS-")
    assert b["scoreboard"] == {"attempts": 0, "blocked_claims": 0, "refused_actions": 0, "escalations": 0, "forbidden_executed": 0,
                               "fine_marks": 0, "label": "SIMULATED"}
    assert b["usage"] == {"messages": 0, "recent_messages": 0, "tokens": 0, "cost_paise": 0}
    assert b["caps"]["max_messages"] == 40 and b["caps"]["burst_messages"] == 8


def test_message_response_is_the_production_shape_plus_scoreboard(setup):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    body = say(api, prov, sid, "my settlement has not come", [decision(None, [STATUS]), render(STATUS)], language="en-IN")
    assert body["dropped"] is False and body["event_id"].startswith("MM-")
    assert body["replies"][0]["text"].endswith("Settlements status is PENDING.")
    assert body["scoreboard"]["attempts"] == 1 and body["session"]["session_id"] == sid


def test_validation_errors_are_422(setup):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    assert api.post(f"/api/judge/sessions/{sid}/messages", json={"text": ""}).status_code == 422
    assert api.post(f"/api/judge/sessions/{sid}/feedback", json={"verdict": "bad"}).status_code == 422
    assert api.post("/api/judge/sessions", json={}).status_code == 422


def test_break_it_card_lists_attacks_in_four_languages_and_flags_unreviewed_text(setup):
    api, stack, prov = setup()
    card = api.get("/api/judge/break-it").json()
    assert card["label"] == "SIMULATED"
    langs = {t["language"] for c in card["cards"] for t in c["tries"]}
    assert {"en", "hi", "hinglish", "kn"} <= langs
    assert all(t.get("review") == "native review pending" for c in card["cards"] for t in c["tries"] if t["language"] in ("hi", "kn"))
    assert {"otp", "bank", "inject"} <= {c["id"] for c in card["cards"]}


def test_get_session_is_read_only(setup):
    api, stack, prov = setup()
    sid = start(api, "P-1")
    n = stack.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    api.get(f"/api/judge/sessions/{sid}")
    assert stack.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == n
