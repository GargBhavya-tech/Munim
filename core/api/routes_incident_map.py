"""GET /api/incident-map: aggregated, privacy-floored, SIMULATED incident map data (#125). Read-only. NOT registered in app.py yet."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from core.api.deps import read_db
from jobs.incident_map import build_incident_map

router = APIRouter(prefix="/api")


@router.get("/incident-map")
def incident_map(status: str = "OPEN", conn=Depends(read_db)):
    if status not in ("OPEN", "RESOLVED", "ALL"):
        raise HTTPException(422, "status must be OPEN, RESOLVED or ALL")
    return build_incident_map(conn, status)
