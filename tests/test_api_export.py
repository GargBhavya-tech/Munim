"""HTTP contract of /api/export/* (#136). The router is mounted here (the lead registers it in core/api/app.py)."""
import json

import pytest

from core.api import routes_export
from scripts import verify_pack as vp
from tests.test_judge_mode import say, setup, start  # noqa: F401  (setup is a fixture)
from tests.test_turn_assembly import STATUS, decision, render


@pytest.fixture
def api_run(setup):
    api, stack, prov = setup()
    api.app.include_router(routes_export.router)
    sid = start(api, "P-1")
    say(api, prov, sid, "my settlement has not come", [decision(None, [STATUS]), render(STATUS)], language="en")
    return api, stack, sid


def test_json_export_is_an_attachment_that_verifies_and_matches_the_live_scoreboard(api_run):
    api, stack, sid = api_run
    r = api.get(f"/api/export/judge/{sid}")
    assert r.status_code == 200 and f'filename="evidence-{sid}.json"' in r.headers["content-disposition"]
    pack = r.json()
    assert pack["simulated"] is True and vp.verify(pack)[0] is True
    assert pack["scoreboard"] == api.get(f"/api/judge/sessions/{sid}").json()["scoreboard"]


def test_html_export_is_a_labelled_attachment_the_verifier_can_read(api_run):
    api, _, sid = api_run
    r = api.get(f"/api/export/judge/{sid}?format=html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "SIMULATED" in r.text
    assert vp.verify(vp.load(r.text))[0] is True


def test_run_export_covers_the_whole_ledger(api_run):
    api, stack, _ = api_run
    pack = api.get("/api/export/run").json()
    assert pack["ledger"][0]["seq"] == 1 and pack["window"]["entries"] == len(stack.ledger.entries()) and vp.verify(pack)[0]


def test_errors(api_run):
    api, _, _ = api_run
    assert api.get("/api/export/judge/JS-99999").status_code == 404
    assert api.get("/api/export/judge/nonsense").status_code == 404
    assert api.get("/api/export/judge/JS-1?format=pdf").status_code == 422


def test_export_is_read_only_and_refuses_with_409_on_a_leak(api_run):
    api, stack, sid = api_run
    n = stack.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    api.get(f"/api/export/judge/{sid}")
    assert stack.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == n
    stack.ledger.append(kind="STATE", actor="SYSTEM", payload={"note": "otp 123456"})
    r = api.get(f"/api/export/judge/{sid}")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "PACK_LEAK" and "123456" not in r.text
