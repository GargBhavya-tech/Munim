"""Evidence pack export (#136). NOT registered in core/api/app.py by this ticket; see the report for the registration lines.

GET /api/export/judge/{sid}?format=json|html   -> the session's evidence pack (attachment)
GET /api/export/run?format=json|html           -> the whole ledger as an evidence pack
422 on a bad format, 404 UNKNOWN_SESSION, 409 PACK_LEAK when the export would contain secret-like text (nothing is sent).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.api.deps import read_db
from core.audit import session_pack as sp
from core.judge import export as ex
from core.judge import session as sess

router = APIRouter(prefix="/api/export")


def _respond(pack: dict, fmt: str, name: str):
    if fmt == "html":
        return HTMLResponse(sp.render_html(pack), headers={"Content-Disposition": f'attachment; filename="{name}.html"'})
    return JSONResponse(pack, headers={"Content-Disposition": f'attachment; filename="{name}.json"'})


@router.get("/judge/{sid}")
def export_judge(sid: str, request: Request, format: str = Query("json", pattern="^(json|html)$"), conn=Depends(read_db)):
    try:
        s = sess.load_session(conn, sid)
    except sess.UnknownSession:
        raise HTTPException(404, {"error": "UNKNOWN_SESSION"})
    try:
        pack = ex.export_session(conn, request.app.state.clock, s)
    except sp.PackLeak as e:
        raise HTTPException(409, {"error": "PACK_LEAK", "detail": str(e)})
    return _respond(pack, format, f"evidence-{sid}")


@router.get("/run")
def export_run(request: Request, format: str = Query("json", pattern="^(json|html)$"), conn=Depends(read_db)):
    try:
        pack = ex.export_run(conn, request.app.state.clock)
    except sp.PackLeak as e:
        raise HTTPException(409, {"error": "PACK_LEAK", "detail": str(e)})
    return _respond(pack, format, "evidence-run")
