"""#117 MCP server: tool list generated from the registry; every call through the executor + policy (MCP-EXPOSE-001)."""
import io
import json

import pytest

from core.actions.registry import FORBIDDEN_ACTIONS, ActionRegistry, ActionSpec, RiskClass
from integrations.mcp import server as srv
from integrations.mcp.server import McpServer, tool_listing
from packs.paytm_merchant.actions import build_registry
from packs.paytm_merchant.policy.rules import build_engine
from tests.test_executor import SpyHttp, make_executor

M = "M-0001"


@pytest.fixture
def spy(http, conn):
    return SpyHttp(http, conn)


@pytest.fixture
def ex(db_path, clock, spy):
    e = make_executor(db_path, clock, spy)
    e.policy = build_engine(e.conn, clock, e.registry)
    spy.conn = e.conn
    yield e
    e.conn.close()


def rpc(server, method, params=None, rid=1):
    return server.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def call(server, name, args=None):
    return rpc(server, "tools/call", {"name": name, "arguments": args or {}})


def names(server):
    return {t["name"] for t in rpc(server, "tools/list")["result"]["tools"]}


def test_initialize_and_unknown_method(ex):
    s = McpServer(ex)
    r = rpc(s, "initialize")["result"]
    assert r["serverInfo"]["name"] == "munim-mcp" and "tools" in r["capabilities"]
    assert rpc(s, "nope")["error"]["code"] == srv.METHOD_NOT_FOUND
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"id": 1})["error"]["code"] == srv.INVALID_REQUEST


def test_listing_is_reads_and_drafts_only_and_never_forbidden(ex):
    s = McpServer(ex)
    listed = names(s)
    reg = build_registry()
    expect = {x.name for x in reg.specs() if x.cls in (RiskClass.R0, RiskClass.DRAFT) and x.agent_callable}
    assert listed == expect and "get_merchant" in listed and "draft_message" in listed
    assert not listed & FORBIDDEN_ACTIONS
    assert all(reg.get(n).cls in (RiskClass.R0, RiskClass.DRAFT) for n in listed)
    assert not any(reg.get(n).cls is RiskClass.R3 for n in names(McpServer(ex, expose_writes=True)))


def test_writes_listing_excludes_r3_offers_contact_and_compensations(ex):
    reg = build_registry()
    listed = names(McpServer(ex, expose_writes=True))
    assert "raise_ticket" in listed and "save_preference" in listed
    for n in listed:
        sp = reg.get(n)
        assert sp.agent_callable and not sp.offer and not sp.contact and sp.cls is not RiskClass.R3
    assert "apply_fee_waiver" not in listed and "send_message" not in listed and "withdraw_ticket" not in listed


def test_newly_forbidden_action_disappears_from_listing(monkeypatch):
    from pydantic import BaseModel

    class Args(BaseModel):
        merchant_id: str
    reg = ActionRegistry([ActionSpec("get_shiny_thing", RiskClass.R0, Args, description="x")])
    assert [t["name"] for t in tool_listing(reg)] == ["get_shiny_thing"]
    monkeypatch.setattr(srv, "FORBIDDEN_ACTIONS", frozenset(FORBIDDEN_ACTIONS | {"get_shiny_thing"}))
    assert tool_listing(reg) == []


def test_read_goes_through_executor_and_is_ledgered(ex, spy):
    s = McpServer(ex)
    r = call(s, "get_merchant", {"merchant_id": M})["result"]
    assert r["isError"] is False
    body = json.loads(r["content"][0]["text"])
    assert body["merchant_id"] == M
    kinds = [e.kind for e in ex.ledger.entries(None) if e.payload.get("action") == "get_merchant"]
    assert "ACTION_INTENT" in kinds and "ACTION_RESULT" in kinds
    assert spy.calls and all(c[0] == "GET" for c in spy.calls)


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ACTIONS))
def test_forbidden_call_refused_ledgered_no_mock_call(ex, spy, name):
    s = McpServer(ex)
    r = call(s, name, {"merchant_id": M})["result"]
    assert r["isError"] is True
    assert json.loads(r["content"][0]["text"])["status"] == "REFUSED"
    assert spy.calls == []
    pol = [e for e in ex.ledger.entries(None) if e.kind == "POLICY" and e.payload["action"] == name]
    assert len(pol) == 1 and pol[0].payload["allow"] is False


def test_unregistered_and_unlisted_names(ex, spy):
    s = McpServer(ex)
    assert json.loads(call(s, "does_not_exist")["result"]["content"][0]["text"])["status"] == "REFUSED"
    assert call(s, "restart_device", {"merchant_id": M})["error"]["code"] == srv.INVALID_PARAMS   # R3: not listed
    assert call(s, "raise_ticket", {"merchant_id": M})["error"]["code"] == srv.INVALID_PARAMS     # write: not listed by default
    assert spy.calls == []


TICKET = {"merchant_id": M, "type": "OTHER", "system_object": "x", "summary": "s", "_case_id": "C-1"}


def test_write_over_mcp_default_denied_without_allowlist(ex, spy):
    s = McpServer(ex, expose_writes=True)
    r = call(s, "raise_ticket", TICKET)["result"]
    assert r["isError"] is True
    assert spy.writes() == []
    assert json.loads(r["content"][0]["text"])["status"] == "REFUSED"


def test_write_with_step_allowlist_executes_once_idempotently(ex, spy):
    s = McpServer(ex, expose_writes=True, allowlist_for=lambda c: ("raise_ticket",))
    a = call(s, "raise_ticket", TICKET)["result"]
    b = call(s, "raise_ticket", TICKET)["result"]
    assert a["isError"] is False, a
    assert b["isError"] is False
    assert len(spy.writes()) == 1


def test_stdio_framing(ex):
    s = McpServer(ex)
    inp = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        ""]))
    out = io.StringIO()
    s.serve(inp, out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert len(lines) == 2 and lines[0]["id"] == 1 and lines[1]["error"]["code"] == srv.PARSE_ERROR


def test_server_never_touches_mocks_directly():
    from pathlib import Path
    src = Path(srv.__file__).read_text(encoding="utf-8")
    assert ".request(" not in src and "TestClient" not in src and "httpx" not in src and "mocks" not in src
