"""#125 incident map: aggregates to pincode, privacy floor of 5, SIMULATED, no exact merchant locations, read-only API router."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import routes_incident_map
from jobs import incident_map as im
from jobs.incidents import ensure_schema


def seed(conn, groups, status="OPEN"):
    """groups: {pincode: n merchants}. One incident per pincode, merchants taken from the seeded table with their pincode rewritten."""
    ensure_schema(conn)
    mids = [r[0] for r in conn.execute("SELECT merchant_id FROM merchants ORDER BY merchant_id")]
    i = 0
    for n_inc, (pin, n) in enumerate(groups.items()):
        iid = f"INC-T{n_inc}"
        conn.execute("INSERT INTO incidents(incident_id, kind, area, status, opened_at) VALUES (?, 'HEARTBEAT_PINCODE', ?, ?, '2026-10-30T10:00:00+05:30')", (iid, pin, status))
        for _ in range(n):
            conn.execute("UPDATE merchants SET pincode = ?, lat = 12.97123456, lng = 77.59876543 WHERE merchant_id = ?", (pin, mids[i]))
            conn.execute("INSERT INTO incident_members VALUES (?,?,NULL)", (iid, mids[i]))
            i += 1
    return mids


def test_aggregates_to_pincode_with_counts(conn):
    seed(conn, {"560034": 7, "560001": 12})
    m = im.build_incident_map(conn)
    by = {f["properties"]["pincode"]: f["properties"] for f in m["features"]}
    assert by["560034"]["affected_merchants"] == 7 and by["560001"]["affected_merchants"] == 12
    assert m["label"] == "SIMULATED" and m["type"] == "FeatureCollection"


def test_privacy_floor_hides_cells_under_five_merchants(conn):
    seed(conn, {"560034": 4, "560001": 5, "560002": 1})
    m = im.build_incident_map(conn)
    assert [f["properties"]["pincode"] for f in m["features"]] == ["560001"]      # 5 is shown, 4 and 1 are not
    assert m["hidden_cells"] == 2 and m["privacy_floor"] == 5
    assert "560034" not in json.dumps(m) and "560002" not in json.dumps(m)


def test_no_merchant_identity_or_exact_location_in_output(conn):
    mids = seed(conn, {"560034": 6})
    text = json.dumps(im.build_incident_map(conn))
    assert not any(mid in text for mid in mids)
    assert "12.97123" not in text and "77.59876" not in text
    f = im.build_incident_map(conn)["features"][0]
    assert f["properties"]["synthetic_coordinates"] is True
    assert f["geometry"]["coordinates"] == [round(c, 3) for c in f["geometry"]["coordinates"]]
    lng, lat = f["geometry"]["coordinates"]
    assert im.BOX["lat"][0] <= lat <= im.BOX["lat"][1] and im.BOX["lng"][0] <= lng <= im.BOX["lng"][1]


def test_cell_centre_is_deterministic_and_pincode_specific():
    assert im.cell_centre("560034") == im.cell_centre("560034") != im.cell_centre("560035")


def test_same_merchant_in_two_incidents_counts_once(conn):
    mids = seed(conn, {"560034": 5})
    conn.execute("INSERT INTO incidents(incident_id, kind, area, status, opened_at) VALUES ('INC-X','SETTLEMENT_BANK','HDFC','OPEN','2026-10-30T10:00:00+05:30')")
    conn.execute("INSERT INTO incident_members VALUES ('INC-X', ?, NULL)", (mids[0],))
    p = im.build_incident_map(conn)["features"][0]["properties"]
    assert p["affected_merchants"] == 5 and p["incident_count"] == 2 and p["kinds"] == ["HEARTBEAT_PINCODE", "SETTLEMENT_BANK"]


def test_status_filter(conn):
    seed(conn, {"560034": 6}, status="RESOLVED")
    assert im.build_incident_map(conn)["features"] == []
    assert len(im.build_incident_map(conn, "ALL")["features"]) == 1


def test_empty_database_without_incident_tables_gives_empty_map(conn):
    conn.execute("DROP TABLE IF EXISTS incident_members")
    assert im.build_incident_map(conn)["features"] == []


def test_api_router_is_read_only_and_serves_the_map(db_path):
    from core.memory.store import connect
    c = connect(db_path)
    seed(c, {"560034": 6})
    c.close()
    app = FastAPI()
    app.state.db_path = db_path
    app.include_router(routes_incident_map.router)
    cl = TestClient(app)
    r = cl.get("/api/incident-map")
    assert r.status_code == 200 and r.json()["features"][0]["properties"]["affected_merchants"] == 6 and r.json()["label"] == "SIMULATED"
    assert cl.get("/api/incident-map?status=bogus").status_code == 422
    assert cl.post("/api/incident-map").status_code == 405
    assert {m for rt in routes_incident_map.router.routes for m in rt.methods} == {"GET"}
