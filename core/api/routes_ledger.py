"""Ledger pagination and chain verification. Never returns the whole ledger in one response."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from core.api.deps import ledger_json, read_db
from core.audit.ledger import verify_chain

router = APIRouter(prefix="/api/ledger")


@router.get("")
def page(after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), case_id: Optional[str] = None, conn=Depends(read_db)):
    sql, args = "SELECT * FROM ledger WHERE seq > ?", [after_seq]
    if case_id:
        sql += " AND case_id = ?"
        args.append(case_id)
    sql += " ORDER BY seq LIMIT ?"
    args.append(limit)
    entries = [ledger_json(r) for r in conn.execute(sql, args)]
    return {"entries": entries, "next_after_seq": entries[-1]["seq"] if entries else after_seq}


@router.get("/verify")
def verify(conn=Depends(read_db)):
    v = verify_chain(conn)
    return {"ok": v.ok, "entries": v.entries, "first_mismatch": v.first_mismatch, "reason": v.reason}
