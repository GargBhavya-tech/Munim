"""Questions to the ledger (#123): POST /api/ledger/ask {question, role}. Read-only; see core/audit/questions.py for what can be asked."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.audit.questions import NotAllowed, RefusedQuestion, ask
from core.api.deps import open_ro

router = APIRouter(prefix="/api/ledger")


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    role: str


@router.post("/ask")
def ask_ledger(body: AskIn, request: Request):
    conn = open_ro(request.app.state.db_path)
    try:
        return ask(conn, request.app.state.clock, body.question, body.role)
    except NotAllowed as e:
        raise HTTPException(403, {"error": "NOT_ALLOWED", "detail": str(e)})
    except RefusedQuestion as e:
        raise HTTPException(422, {"error": str(e)})
    finally:
        conn.close()
