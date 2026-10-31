"""A SIMULATED road provider for the demo stack (no map key, no network).

``field/travel.py`` refuses to guess: with no real road provider it raises ``TravelUnavailable`` and a route is not planned. The demo has no map service, so this
provider stands in for one and says so: it is named SIMULATED, it is used only where the demo stack wires it, and every plan built on it is labelled simulated.
It is deterministic and time-of-day aware, so "traffic-aware" behaviour can be shown and tested: road distance is the straight-line distance times a detour
factor, and the speed drops in the morning and evening peaks.

All numbers here are [ASSUMPTION]s for a synthetic city; none is a measurement.
"""
from __future__ import annotations

import math
from datetime import datetime

Point = tuple[float, float]
DETOUR = 1.35                                        # roads are not straight lines
FREE_FLOW_KMH = 28.0
PEAKS = ((8 * 60, 10 * 60 + 30, 0.55), (17 * 60, 20 * 60, 0.5))     # (from minute, to minute, share of free-flow speed)
MIN_MINUTES = 3.0


def haversine_km(a: Point, b: Point) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def speed_share(departure: datetime) -> float:
    minute = departure.hour * 60 + departure.minute
    for start, end, share in PEAKS:
        if start <= minute < end:
            return share
    return 1.0


class SimulatedRoadProvider:
    """Implements the ``RoadProvider`` protocol of field/travel.py. SIMULATED."""

    name = "simulated"

    def minutes(self, origin: Point, dest: Point, departure: datetime) -> float:
        km = haversine_km(origin, dest) * DETOUR
        return max(MIN_MINUTES, km / (FREE_FLOW_KMH * speed_share(departure)) * 60.0)
