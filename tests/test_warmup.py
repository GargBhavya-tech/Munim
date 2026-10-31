"""Demo warm-up: one real turn on a throwaway copy, so the first judge does not wait, and the demo database is never touched.
Providers are the scripted fakes (tests/fakes_gate.py); this proves the wiring and the isolation, not model speed. NOT RUN when written (owner asked for no test runs)."""
import pytest

from core.api import dev_server
from core.api.dev_server import attach_llm, warm_up
from core.engine.knowledge_layer import KnowledgeLayer
from tests.fakes_gate import FakeNLI, FakeTranslator, llm
from tests.test_turn_assembly import STATUS, decision, render


class NoHits:
    def search(self, query, scope, k):
        return []


@pytest.fixture
def lang_en(monkeypatch):
    from core.voice import language as L
    monkeypatch.setattr(L.LanguageManager, "current", lambda self, mid, case_language=None: "en-IN")


def live_counts(db_path):
    import sqlite3
    c = sqlite3.connect(str(db_path))
    try:
        return tuple(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("ledger", "events", "cases"))
    finally:
        c.close()


def fake_app(db_path):
    state = type("S", (), {"warm_attach": staticmethod(lambda s: None), "db_path": db_path})()
    return type("A", (), {"state": state})()


def test_warm_up_runs_one_real_turn_and_leaves_the_demo_database_untouched(db_path, lang_en):
    before = live_counts(db_path)
    seen = []

    def attach(stack):
        client, prov = llm([decision(None, [STATUS]), render(STATUS)], stack.ledger)
        seen.append(prov)
        attach_llm(stack, client=client, nli=FakeNLI(), translator=FakeTranslator(), knowledge=KnowledgeLayer(NoHits()))

    r = warm_up(attach, db_path)
    assert r["ok"] and r["llm_ok"] and r["status"] == 200 and r["replies"] == 1 and r["seconds"] >= 0
    assert seen and not seen[0].replies                                      # the scripted model was asked exactly twice: decide, then render
    assert live_counts(db_path) == before                                    # nothing was written to the database the demo uses


def test_a_failing_warm_up_reports_it_and_does_not_stop_the_server_starting(db_path, monkeypatch, capsys):
    def boom(attach, path):
        raise RuntimeError("model download failed")
    served = []
    monkeypatch.setattr(dev_server, "warm_up", boom)
    monkeypatch.setattr(dev_server, "build", lambda *a, **k: (fake_app(db_path), None))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: served.append(1))
    monkeypatch.setattr("sys.argv", ["dev_server", "--llm"])
    dev_server.main()
    out = capsys.readouterr().out
    assert "warm-up failed (RuntimeError" in out and "serving anyway" in out and served == [1]


def test_no_warmup_flag_skips_it(db_path, monkeypatch):
    called = []
    monkeypatch.setattr(dev_server, "warm_up", lambda attach, path: called.append(1) or {"ok": True, "status": 200, "seconds": 0, "replies": 0, "llm_ok": True})
    monkeypatch.setattr(dev_server, "build", lambda *a, **k: (fake_app(db_path), None))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["dev_server", "--llm", "--no-warmup"])
    dev_server.main()
    assert called == []


def test_a_turn_handed_over_because_the_model_was_unreachable_is_not_counted_as_a_good_warm_up(conn):
    """Rehearsal: the key was empty, the one 'reply' was the hand-over line, and the first warm-up printed 'done'."""
    from core.audit.ledger import Ledger
    from core.engine.clock import SimClock
    from core.api.dev_server import llm_failed
    ledger = Ledger(conn, SimClock())
    assert llm_failed(conn) is False
    ledger.append(kind="ESCALATION", actor="SYSTEM", case_id="CS-0001", payload={"reasons": ["settlement.status == 'HELD_RISK'"]})
    assert llm_failed(conn) is False                                                      # an ordinary escalation is not a model failure
    ledger.append(kind="ESCALATION", actor="SYSTEM", case_id="CS-0001", payload={"reasons": ["TOOL_FAILURE: UPSTREAM_UNAVAILABLE: llm"]})
    assert llm_failed(conn) is True
