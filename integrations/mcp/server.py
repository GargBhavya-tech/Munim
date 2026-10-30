"""MCP-protocol-shaped server (#117), JSON-RPC 2.0, no third-party package.

The tool list is GENERATED from the action registry on every call:
  * only agent-callable actions of class R0 (reads) and DRAFT are listed by default;
  * writes (R1/R2, never offers, never contact actions) are listed only when ``expose_writes=True``;
  * R3 is never listed here (it needs an approval, which does not travel over MCP);
  * a name in FORBIDDEN_ACTIONS is never listed, whatever the registry holds.
Every call goes through ``Executor.submit`` with ``facts["via_mcp"]=True`` so MCP-EXPOSE-001 and default-deny apply.
A write over MCP carries no allowlist unless the injected ``allowlist_for(case_id)`` supplies one (the case engine's
current-step allowlist), so by default a write is refused: there is no bypass and the handler never touches a mock.
"""
from __future__ import annotations

import json
from typing import Callable, TextIO

from core.actions.registry import FORBIDDEN_ACTIONS, ActionRegistry, ActionSpec, RiskClass
from core.actions.types import ActionIntent, Principal

PROTOCOL_VERSION = "2024-11-05"
MCP_PRINCIPAL = Principal("AGENT", "mcp")
_READ_LIKE = (RiskClass.R0, RiskClass.DRAFT)
_WRITE_LIKE = (RiskClass.R1, RiskClass.R2)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def exposed_specs(registry: ActionRegistry, *, expose_writes: bool = False) -> list[ActionSpec]:
    classes = _READ_LIKE + (_WRITE_LIKE if expose_writes else ())
    return [s for s in registry.specs()
            if s.name not in FORBIDDEN_ACTIONS and s.agent_callable and not s.offer and not s.contact and s.cls in classes]


def tool_listing(registry: ActionRegistry, *, expose_writes: bool = False) -> list[dict]:
    out = []
    for s in exposed_specs(registry, expose_writes=expose_writes):
        schema = s.args_model.model_json_schema()
        schema.setdefault("type", "object")
        if s.cls in _WRITE_LIKE:
            schema.setdefault("properties", {})["_case_id"] = {"type": "string"}
        out.append({"name": s.name, "description": f"[{s.cls.value}] {s.description}".strip(), "inputSchema": schema})
    return out


class McpServer:
    def __init__(self, executor, *, expose_writes: bool = False,
                 allowlist_for: Callable[[str | None], tuple[str, ...] | None] | None = None):
        self.executor = executor
        self.registry: ActionRegistry = executor.registry
        self.expose_writes = expose_writes
        self.allowlist_for = allowlist_for or (lambda case_id: None)

    # ------------------------------------------------------------------ JSON-RPC
    def handle(self, request) -> dict | None:
        """One decoded JSON-RPC message in, one response dict out (None for a notification)."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return _err(request.get("id") if isinstance(request, dict) else None, INVALID_REQUEST, "invalid request")
        rid, method, params = request.get("id"), request["method"], request.get("params") or {}
        is_note = "id" not in request
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "munim-mcp", "version": "0.1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_listing(self.registry, expose_writes=self.expose_writes)}
            elif method == "tools/call":
                result = self._call(params)
            elif is_note:
                return None
            else:
                return _err(rid, METHOD_NOT_FOUND, f"unknown method {method!r}")
        except _ProtocolError as e:
            return None if is_note else _err(rid, e.code, str(e))
        return None if is_note else {"jsonrpc": "2.0", "id": rid, "result": result}

    def handle_line(self, line: str) -> str | None:
        try:
            msg = json.loads(line)
        except ValueError:
            return json.dumps(_err(None, PARSE_ERROR, "parse error"))
        resp = self.handle(msg)
        return None if resp is None else json.dumps(resp, ensure_ascii=False, sort_keys=True)

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            out = self.handle_line(line)
            if out is not None:
                stdout.write(out + "\n")
                stdout.flush()

    # ------------------------------------------------------------------ tools/call
    def _call(self, params: dict) -> dict:
        name, args = params.get("name"), dict(params.get("arguments") or {})
        if not isinstance(name, str):
            raise _ProtocolError(INVALID_PARAMS, "tool name required")
        listed = {s.name for s in exposed_specs(self.registry, expose_writes=self.expose_writes)}
        known = self.registry.has(name) and name not in FORBIDDEN_ACTIONS
        if name not in listed and known:
            raise _ProtocolError(INVALID_PARAMS, f"unknown tool {name!r}")
        # forbidden / unregistered names still go to the executor so the refusal is ledgered (REGISTRY + MCP-EXPOSE-001)
        case_id = args.pop("_case_id", None)
        spec = self.registry.get(name) if name in listed else None
        is_write = spec is not None and spec.cls in _WRITE_LIKE
        intent = ActionIntent(name, args, case_id=case_id, principal=MCP_PRINCIPAL, facts={"via_mcp": True},
                              allowlist=self.allowlist_for(case_id) if is_write else None)
        out = self.executor.submit(intent)
        ok = out.status in ("OK", "REPLAYED")
        body = out.result if ok else {"status": out.status, "error": out.error,
                                      "rule_ids": [v.rule_id for v in out.violations]}
        return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False, sort_keys=True)}], "isError": not ok}


class _ProtocolError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}
