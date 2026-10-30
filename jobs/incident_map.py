"""Bengaluru incident map data (Build Map #125, DETACHABLE). Aggregates the I1 incidents (jobs/incidents.py) by PINCODE.

  * One GeoJSON-like Point feature per pincode cell, at a SYNTHETIC cell centre derived from the pincode (a stable hash inside a Bengaluru-sized
    box, rounded to 3 decimals). Merchant coordinates (merchants.lat/lng) are never read or output.
  * Privacy floor: a cell with fewer than ``MIN_MERCHANTS`` (5) distinct affected merchants is not shown at all; only the NUMBER of hidden cells
    is reported. No merchant id, name or phone is ever in the output.
  * Affected merchants of an incident are its incident_members rows joined to merchants.pincode (a bank incident also lands on pincodes).
  * Labelled SIMULATED. Read-only.
"""
from __future__ import annotations

import hashlib
import sqlite3

LABEL = "SIMULATED"
MIN_MERCHANTS = 5
BOX = {"lat": (12.85, 13.10), "lng": (77.45, 77.75)}       # synthetic Bengaluru-sized box


def cell_centre(pincode: str) -> tuple[float, float]:
    """A deterministic fake centre for a pincode: not a real location of anything."""
    h = hashlib.sha256(f"cell|{pincode}".encode("utf-8")).digest()
    f = lambda b, lo_hi: round(lo_hi[0] + (int.from_bytes(b, "big") / 65535) * (lo_hi[1] - lo_hi[0]), 3)
    return f(h[0:2], BOX["lat"]), f(h[2:4], BOX["lng"])


def build_incident_map(conn: sqlite3.Connection, status: str = "OPEN", min_merchants: int = MIN_MERCHANTS) -> dict:
    """status: OPEN | RESOLVED-like (any incidents.status value) | ALL."""
    sql = ("SELECT i.incident_id, i.kind, m.pincode, im.merchant_id FROM incidents i "
           "JOIN incident_members im ON im.incident_id = i.incident_id JOIN merchants m ON m.merchant_id = im.merchant_id")
    args: tuple = ()
    if status != "ALL":
        sql += " WHERE i.status = ?"
        args = (status,)
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:                 # no incident tables yet: an empty map
        rows = []
    cells: dict[str, dict] = {}
    for r in rows:
        c = cells.setdefault(r[2], {"merchants": set(), "incidents": set(), "kinds": set()})
        c["merchants"].add(r[3])
        c["incidents"].add(r[0])
        c["kinds"].add(r[1])
    features, hidden = [], 0
    for pin in sorted(cells):
        c = cells[pin]
        if len(c["merchants"]) < min_merchants:
            hidden += 1
            continue
        lat, lng = cell_centre(pin)
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
                         "properties": {"pincode": pin, "affected_merchants": len(c["merchants"]), "incident_count": len(c["incidents"]),
                                        "kinds": sorted(c["kinds"]), "synthetic_coordinates": True}})
    return {"type": "FeatureCollection", "label": LABEL, "privacy_floor": min_merchants, "hidden_cells": hidden, "status": status,
            "features": features}
