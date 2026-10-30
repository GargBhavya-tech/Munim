"""Cases: list, detail (timeline, actions, claims, messages), cost, and the approvals list."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from core.actions.types import Principal
from core.engine import merchant_undo
from core.api.deps import case_json, future, ledger_json, read_db, rows

router = APIRouter(prefix="/api")

# Cost assumptions (Bible section 41). Rupees per case for a human agent, and the published Sarvam rates in rupees per
# million tokens. config/sarvam.yaml carries no price, so these are constants here, labelled in the response.
HUMAN_BASELINE_INR = {"low": 24, "mid": 60, "high": 150}
AI_INR_PER_MTOK_IN = 15
AI_INR_PER_MTOK_OUT = 60
LEVEL_COLOR = {"CORROBORATED": "green", "SUPPORTED": "green", "UNSUPPORTED": "red", "CONTRADICTED": "red"}


@router.get("/health")
def health(request: Request):
    return {"ok": True, "sim_time": request.app.state.clock.now_iso()}


@router.get("/cases")
def list_cases(state: Optional[str] = None, lane: Optional[str] = None, merchant_id: Optional[str] = None, conn=Depends(read_db)):
    sql, args = "SELECT * FROM cases WHERE 1=1", []
    for col, val in (("state", state), ("lane", lane), ("merchant_id", merchant_id)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " ORDER BY opened_at DESC, case_id DESC"
    return {"cases": [case_json(r) for r in conn.execute(sql, args)]}


def _actions(conn, clock, case_id: str) -> list[dict]:
    out = []
    for a in conn.execute("SELECT a.*, c.compensable_until AS cu, c.status AS cstatus FROM actions a LEFT JOIN compensations c ON c.action_id = a.action_id"
                          " WHERE a.case_id = ? ORDER BY a.rowid", (case_id,)):
        cu = a["cu"]
        open_win = cu is not None and cu.startswith("9999-")
        undoable = a["status"] == "OK" and a["cstatus"] == "OPEN" and future(cu, clock)
        out.append({"action_id": a["action_id"], "name": a["name"], "cls": a["cls"], "status": a["status"], "idempotency_key": a["idempotency_key"],
                    "compensable_until": None if (cu is None or open_win) else cu, "undoable": undoable})
    return out


def _claims(timeline: list[dict]) -> list[dict]:
    out = []
    for e in timeline:
        if e["kind"] != "VERIFY":
            continue
        for i, c in enumerate(e["payload"].get("claims") or []):
            level = c.get("level", "UNSUPPORTED")
            color = LEVEL_COLOR.get(level, "red")
            if color == "green" and c.get("sent") is False:
                color = "amber"                      # verified, but removed before it reached the merchant
            out.append({"id": c.get("id", f"{e['seq']}.{i + 1}"), "text": c.get("text", ""), "source": c.get("source", ""), "level": level,
                        "color": color, "reason": c.get("reason"), "rule_id": c.get("rule_id"), "verify_seq": e["seq"],
                        "nli_label": c.get("nli_label"), "nli_score": c.get("nli_score"), "snippet": c.get("snippet"),
                        "corroborated_by": c.get("corroborated_by") or []})
    return out


def _renders(timeline: list[dict]) -> list[dict]:
    """Every gate run's sentences with their back-translation, for the case view's 'English back-translation' toggle."""
    return [{"verify_seq": e["seq"], "status": e["payload"].get("status"), "language": e["payload"].get("language"),
             "sentences": [{"kind": s.get("kind"), "text": s.get("text"), "claim_ids": s.get("claim_ids") or [], "ok": s.get("ok", True),
                            "back_translation": s.get("back_translation")} for s in e["payload"].get("sentences") or []]}
            for e in timeline if e["kind"] == "VERIFY"]


def _messages(conn, case_id: str, timeline: list[dict]) -> list[dict]:
    msgs = [{"ts_sim": e["ts_sim"], "direction": "OUT", "language": e["payload"].get("language"), "text": e["payload"].get("text", "")}
            for e in timeline if e["kind"] == "MESSAGE_SENT"]
    for r in conn.execute("SELECT * FROM events WHERE type = 'MERCHANT_MESSAGE' AND (case_id = ? OR event_id IN (SELECT event_id FROM case_events WHERE case_id = ?))",
                          (case_id, case_id)):
        p = json.loads(r["payload_json"])
        msgs.append({"ts_sim": r["ts_sim"], "direction": "IN", "language": p.get("language"), "text": p.get("text", "")})
    return sorted(msgs, key=lambda m: m["ts_sim"])


@router.get("/cases/{case_id}")
def get_case(case_id: str, request: Request, conn=Depends(read_db)):
    c = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if c is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    timeline = [ledger_json(r) for r in conn.execute("SELECT * FROM ledger WHERE case_id = ? ORDER BY seq", (case_id,))]
    return {"case": case_json(c), "timeline": timeline, "actions": _actions(conn, request.app.state.clock, case_id),
            "claims": _claims(timeline), "messages": _messages(conn, case_id, timeline), "renders": _renders(timeline),
            "receipts": [r.__dict__ for r in merchant_undo.receipts(conn, request.app.state.clock, case_id, c["language"] or "en-IN")]}


@router.get("/cases/{case_id}/cost")
def case_cost(case_id: str, conn=Depends(read_db)):
    if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    tin = tout = 0
    for r in conn.execute("SELECT payload_json FROM ledger WHERE case_id = ? AND kind IN ('DECISION', 'CONTEXT_HASH')", (case_id,)):
        p = json.loads(r[0])
        tin += int(p.get("tokens_in") or 0)
        tout += int(p.get("tokens_out") or 0)
    ai = (tin * AI_INR_PER_MTOK_IN + tout * AI_INR_PER_MTOK_OUT) / 1_000_000
    return {"ai_inr": ai, "human_baseline_inr": dict(HUMAN_BASELINE_INR),
            "assumptions": ["Human baseline is an assumption: Rs 24 (low), Rs 60 (mid), Rs 150 (high) per handled case (Bible section 41).",
                            f"AI price uses the published Sarvam rates as constants: Rs {AI_INR_PER_MTOK_IN} per million input tokens, "
                            f"Rs {AI_INR_PER_MTOK_OUT} per million output tokens.",
                            "Tokens are summed from DECISION and CONTEXT_HASH ledger entries (payload.tokens_in / tokens_out; the LLM client writes one CONTEXT_HASH "
                            "per model call); entries without them count as zero."],
            "usage": {"tokens_in": tin, "tokens_out": tout}}


@router.post("/actions/{action_id}/undo")
def undo_action(action_id: str, body: dict, request: Request):
    pid, role = body.get("principal_id"), body.get("role")
    if not pid or not role:
        raise HTTPException(422, "principal_id and role are required")
    st = request.app.state
    with st.lock:
        out = st.executor.undo(action_id, Principal("HUMAN", pid, role))
    return {"status": out.status, "action_id": action_id, "error": out.error,
            "violations": [{"rule_id": v.rule_id, "title": v.title, "severity": v.severity, "reason": v.reason} for v in (out.violations or [])]}


@router.get("/approvals")
def list_approvals(status: Optional[str] = "PENDING", conn=Depends(read_db)):
    sql, args = "SELECT approval_id, case_id, view_json, status, role_required FROM approvals", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY rowid"
    return {"approvals": [{"approval_id": r["approval_id"], "case_id": r["case_id"], "action": json.loads(r["view_json"]).get("action"),
                           "status": r["status"], "role_required": r["role_required"]} for r in conn.execute(sql, args)]}


@router.get("/cases/{case_id}/replay")
def case_replay(case_id: str, conn=Depends(read_db)):
    """#67: replay the case from the ledger alone (no model, no network) after re-verifying the whole chain."""
    from core.audit.replay import ReplayError, replay_case
    try:
        r = replay_case(conn, case_id)
    except ReplayError:
        raise HTTPException(404, "CASE_NOT_FOUND")
    return {"case_id": case_id, "chain": {"ok": r.chain.ok, "entries": r.chain.entries, "first_mismatch": r.chain.first_mismatch, "reason": r.chain.reason},
            "state": r.state, "state_hash": r.state_hash, "steps": r.steps}


@router.get("/cases/{case_id}/evidence")
def case_evidence(case_id: str, format: str = "html", conn=Depends(read_db)):
    """#136: the case's evidence pack, as a self-verifying HTML page (default) or JSON."""
    from fastapi.responses import HTMLResponse
    from core.audit.evidence import build_pack, render_html
    try:
        pack = build_pack(conn, case_id)
    except KeyError:
        raise HTTPException(404, "CASE_NOT_FOUND")
    if format == "json":
        return pack
    return HTMLResponse(render_html(pack), headers={"Content-Disposition": f'inline; filename="evidence-{case_id}.html"'})
