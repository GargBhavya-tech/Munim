"""Road travel times (#106) for routing.

Every time comes from a ROAD provider with a departure time (a Maps API for traffic, OSRM as the fallback). There is deliberately no
straight-line fallback: a crow-flies minute count makes a one-way street or a river look like a short hop and produces routes that cannot be
driven. If every provider fails the call raises ``TravelUnavailable`` and the caller says so.

The matrix is NOT symmetric: each ordered pair (A to B, B to A) is asked and cached separately, keyed by rounded coordinates and a departure
bucket (weekday or weekend plus the hour), so the same trip at rush hour and at night are different entries. Repeats are cache hits.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Protocol, Sequence

import httpx

SCHEMA = "CREATE TABLE IF NOT EXISTS travel_cache (key TEXT PRIMARY KEY, minutes REAL NOT NULL, provider TEXT NOT NULL, bucket TEXT NOT NULL);"
Point = tuple[float, float]


class TravelUnavailable(Exception):
    pass


class RoadProvider(Protocol):
    name: str

    def minutes(self, origin: Point, dest: Point, departure: datetime) -> float: ...


def bucket(departure: datetime) -> str:
    """Departure bucket: weekday/weekend and the hour. Traffic differs by both."""
    return f"{'we' if departure.weekday() >= 5 else 'wd'}-{departure.hour:02d}"


def cache_key(origin: Point, dest: Point, b: str) -> str:
    return f"{origin[0]:.5f},{origin[1]:.5f}>{dest[0]:.5f},{dest[1]:.5f}@{b}"


class OsrmProvider:
    """OSRM route service (no traffic data). ``http`` is an httpx.Client whose base_url is the OSRM server."""
    name = "osrm"

    def __init__(self, http: httpx.Client, profile: str = "driving"):
        self.http, self.profile = http, profile

    def minutes(self, origin: Point, dest: Point, departure: datetime) -> float:
        r = self.http.get(f"/route/v1/{self.profile}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}", params={"overview": "false"}, timeout=10)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != "Ok" or not body.get("routes"):
            raise TravelUnavailable(f"osrm: {body.get('code')}")
        return body["routes"][0]["duration"] / 60.0


class MapsProvider:
    """Google Distance Matrix with departure_time, so the time includes traffic. The key comes from MAPS_API_KEY."""
    name = "maps"

    def __init__(self, http: httpx.Client, api_key: str):
        self.http, self.api_key = http, api_key

    def minutes(self, origin: Point, dest: Point, departure: datetime) -> float:
        r = self.http.get("/maps/api/distancematrix/json", timeout=10, params={
            "origins": f"{origin[0]},{origin[1]}", "destinations": f"{dest[0]},{dest[1]}", "mode": "driving",
            "departure_time": int(departure.timestamp()), "key": self.api_key})
        r.raise_for_status()
        el = r.json()["rows"][0]["elements"][0]
        if el.get("status") != "OK":
            raise TravelUnavailable(f"maps: {el.get('status')}")
        return (el.get("duration_in_traffic") or el["duration"])["value"] / 60.0


class TravelMatrix:
    def __init__(self, conn: sqlite3.Connection, providers: Sequence[RoadProvider]):
        if not providers:
            raise ValueError("at least one road provider is required; there is no straight-line fallback")
        self.conn, self.providers = conn, list(providers)
        conn.executescript(SCHEMA)

    def minutes(self, origin: Point, dest: Point, departure: datetime) -> int:
        if (round(origin[0], 5), round(origin[1], 5)) == (round(dest[0], 5), round(dest[1], 5)):
            return 0
        b = bucket(departure)
        k = cache_key(origin, dest, b)
        row = self.conn.execute("SELECT minutes FROM travel_cache WHERE key = ?", (k,)).fetchone()
        if row is not None:
            return math.ceil(row["minutes"])
        errors = []
        for p in self.providers:
            try:
                m = float(p.minutes(origin, dest, departure))
            except Exception as e:     # noqa: BLE001 - any provider failure moves on to the next
                errors.append(f"{p.name}: {type(e).__name__}")
                continue
            if m < 0 or math.isnan(m):
                errors.append(f"{p.name}: invalid time")
                continue
            self.conn.execute("INSERT OR REPLACE INTO travel_cache(key, minutes, provider, bucket) VALUES (?,?,?,?)", (k, m, p.name, b))
            return math.ceil(m)
        raise TravelUnavailable("no road provider answered: " + "; ".join(errors))

    def matrix(self, points: Sequence[Point], departure: datetime) -> list[list[int]]:
        """Whole minutes, row = from, column = to. Each ordered pair is asked separately: the result is not assumed symmetric."""
        return [[0 if i == j else self.minutes(a, b, departure) for j, b in enumerate(points)] for i, a in enumerate(points)]
