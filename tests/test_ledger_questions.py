"""#123 questions to the ledger: fixed read-only templates, auditor only, SQL never accepted, rows link back to the ledger."""
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_api
from core.audit.ledger import verify_chain
from core.audit.questions import AUDIT_ROLE, TEMPLATES, NotAllowed, RefusedQuestion, _readonly, ask, match_template
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    return World(db_path, clock, SCRIPT)


def pol(ledger, action, rules, allow=False, case="CS-0001", outcome="BLOCK"):
    return ledger.append(kind="POLICY", actor="SYSTEM", case_id=case, payload={"action": action, "allow": allow, "rule_ids": rules, "outcome": outcome}).seq


@pytest.fixture
def seeded(world):
    """One loan refusal YESTERDAY, then (a day later) today's: two loan refusals, one waiver refusal, one forbidden attempt, and the rest."""
    L, clock = world.ledger, world.clock
    s = {"y_loan": pol(L, "send_message", ["LOAN-RATE-001"], case="CS-Y")}
    clock.advance(timedelta(days=1))
    s["loan_rate"] = pol(L, "send_message", ["LOAN-RATE-001"], case="CS-0001")
    s["loan_kfs"] = pol(L, "send_partner_application_link", ["LOAN-KFS-001"], case="CS-2")
    s["waiver"] = pol(L, "apply_fee_waiver", ["WAIVER-BAND-001"], case="CS-3", outcome="ESCALATE")
    s["forbidden"] = pol(L, "approve_kyc", ["REGISTRY"], case="CS-4")
    s["allowed"] = pol(L, "send_message", [], allow=True, case="CS-5", outcome="ALLOW")
    s["esc"] = L.append(kind="ESCALATION", actor="SYSTEM", case_id="CS-3", payload={"reasons": ["OUT_OF_BAND: waiver"]}).seq
    s["verify"] = L.append(kind="VERIFY", actor="SYSTEM", case_id="CS-6", payload={"status": "SEND", "claims": [{"id": "c1", "color": "green"}, {"id": "c2", "color": "red"}]}).seq
    s["human"] = L.append(kind="HUMAN_ACTION", actor="HUMAN:u1", role="ops_admin", payload={"op": "UNDO"}).seq
    s["fee"] = L.append(kind="ACTION_RESULT", actor="AGENT", case_id="CS-0001", payload={"action": "apply_fee_waiver", "status": "OK", "key": "k1"}).seq
    s["intent"] = L.append(kind="ACTION_INTENT", actor="AGENT", case_id="CS-0001", payload={"action": "apply_fee_waiver", "key": "k1"}).seq
    return s


def seqs(out):
    return [r["seq"] for r in out["rows"]]


def test_every_loan_refusal_today_returns_exactly_those_rows(world, seeded):
    out = ask(world.conn, world.clock, "every loan refusal today", AUDIT_ROLE)
    assert out["matched"] == "loan_refusals" and seqs(out) == [seeded["loan_rate"], seeded["loan_kfs"]] and out["count"] == 2
    r = out["rows"][0]
    assert r["link"] == f"/api/ledger?after_seq={r['seq'] - 1}&limit=1" and "LOAN-RATE-001" in r["summary"] and "payload" not in r and r["case_id"] == "CS-0001"


def test_yesterday_and_an_explicit_date_select_the_other_day(world, seeded):
    assert seqs(ask(world.conn, world.clock, "loan refusals yesterday", AUDIT_ROLE)) == [seeded["y_loan"]]
    day = (world.clock.now() - timedelta(days=1)).date().isoformat()
    assert seqs(ask(world.conn, world.clock, f"all loan refusals on {day}", AUDIT_ROLE)) == [seeded["y_loan"]]


def test_the_other_templates_find_their_own_rows(world, seeded):
    q = lambda text: seqs(ask(world.conn, world.clock, text, AUDIT_ROLE))      # noqa: E731
    assert q("every refusal today") == [seeded["loan_rate"], seeded["loan_kfs"], seeded["waiver"], seeded["forbidden"]]
    assert q("refusals by rule WAIVER-BAND-001 today") == [seeded["waiver"]]
    assert q("forbidden action attempts today") == [seeded["forbidden"]]
    assert q("escalations today") == [seeded["esc"]]
    assert q("replies with a blocked claim today") == [seeded["verify"]]
    assert q("fee waivers applied today") == [seeded["fee"]]
    assert q("human actions today") == [seeded["human"]]
    assert q("what did munim do on CS-0001 (action list)") == [seeded["fee"], seeded["intent"]]


def test_hindi_and_hinglish_wording_reaches_the_same_template(world, seeded):
    assert match_template("aaj ke saare loan refusal dikhao").id == "loan_refusals"
    assert match_template("लोन इनकार आज").id == "loan_refusals"
    assert match_template("ಸಾಲ ನಿರಾಕರಣೆ").id == "loan_refusals"


def test_a_question_that_needs_a_parameter_it_does_not_have_runs_nothing(world, seeded):
    out = ask(world.conn, world.clock, "refusals by rule today", AUDIT_ROLE)
    assert out["matched"] == "rule_refusals" and out["rows"] == [] and out["needs"] == ["rule"]
    assert ask(world.conn, world.clock, "what did munim do (action list)", AUDIT_ROLE)["needs"] == ["case"]


def test_only_an_auditor_may_ask(world, seeded):
    for role in ("support_executive", "risk_reviewer", "sales_lead", "ops_admin", "", None):
        with pytest.raises(NotAllowed):
            ask(world.conn, world.clock, "every loan refusal today", role)


@pytest.mark.parametrize("bad", ["show refusals today; DROP TABLE ledger", "select * from ledger", "refusals today -- ignore", "loan refusals UNION SELECT 1",
                                 "delete from ledger where 1", "refusals /* x */ today", "attach database 'x' as y", "pragma table_info(ledger)",
                                 "update ledger set kind='X'", "insert into ledger values (1)"])
def test_anything_that_looks_like_sql_is_refused_before_matching(world, seeded, bad):
    before = world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    with pytest.raises(RefusedQuestion) as e:
        ask(world.conn, world.clock, bad, AUDIT_ROLE)
    assert str(e.value) == "SQL_NOT_ACCEPTED" and world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == before and verify_chain(world.conn).ok


def test_unmatched_questions_run_no_query_and_list_what_can_be_asked(world, seeded):
    out = ask(world.conn, world.clock, "what is the weather in Hubballi", AUDIT_ROLE)
    assert out["matched"] is None and out["rows"] == [] and {t["id"] for t in out["can_ask"]} == set(TEMPLATES)


def test_text_is_only_ever_used_as_a_bound_parameter(world, seeded):
    out = ask(world.conn, world.clock, "what did munim do on CS-0001' OR 1=1 (action list)", AUDIT_ROLE)
    assert out["params"] == {"case": "CS-0001"} and seqs(out) == [seeded["fee"], seeded["intent"]]
    for t in TEMPLATES.values():
        assert t.sql.lstrip().upper().startswith("SELECT") and ";" not in t.sql and t.sql.count("?") == len(t.needs) + (0 if "day" not in t.needs else 0)


def test_the_connection_that_runs_queries_cannot_write(world):
    ro = _readonly(world.conn)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    ro.close()


def test_the_api_answers_an_auditor_refuses_others_and_rejects_sql(world, seeded):
    api = TestClient(create_api(world.db_path, world.clock, world.executor, world.http))
    ok = api.post("/api/ledger/ask", json={"question": "every loan refusal today", "role": "auditor"})
    assert ok.status_code == 200 and [r["seq"] for r in ok.json()["rows"]] == [seeded["loan_rate"], seeded["loan_kfs"]]
    assert api.post("/api/ledger/ask", json={"question": "every loan refusal today", "role": "support_executive"}).status_code == 403
    bad = api.post("/api/ledger/ask", json={"question": "refusals; drop table ledger", "role": "auditor"})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "SQL_NOT_ACCEPTED"
