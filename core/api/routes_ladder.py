"""Rollout ladder (#148): GET /api/ladder -> every playbook with its current level (L0 shadow, L1 assisted, L2 autonomous). Read-only; levels change only
through core/eval/ladder.py, on evidence."""
from __future__ import annotations

import sqlite3

import yaml
from fastapi import APIRouter, Request

from core.api.deps import open_ro

router = APIRouter(prefix="/api")


@router.get("/ladder")
def ladder(request: Request):
    st = request.app.state
    ids = sorted(yaml.safe_load(p.read_text(encoding="utf-8"))["id"] for p in (st.pack_dir / "playbooks").glob("*.yaml"))
    conn = open_ro(st.db_path)
    try:
        try:
            rows = {r["playbook_id"]: dict(r) for r in conn.execute("SELECT * FROM rollout_levels")}
        except sqlite3.OperationalError as e:               # no ladder table yet: every playbook is at L0. Any other error is raised, not shown as L0.
            if "no such table" not in str(e):
                raise
            rows = {}
    finally:
        conn.close()
    return {"label": "SIMULATED", "levels": {"L0": "shadow", "L1": "assisted", "L2": "autonomous"},
            "playbooks": [{"playbook_id": p, "level": rows.get(p, {}).get("level", "L0"), "reason": rows.get(p, {}).get("reason"),
                           "updated_at": rows.get(p, {}).get("updated_at")} for p in ids]}
