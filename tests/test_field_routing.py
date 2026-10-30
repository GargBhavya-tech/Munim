"""#106 road travel times and #107 the routing engine (gate G4: the heuristic is checked against an exact optimum)."""
import random
from datetime import datetime, timezone
from itertools import combinations, permutations
from pathlib import Path

import httpx
import pytest

from field.routing import HORIZON, Plan, Stop, Vehicle, replan, solve_exact, solve_routes
from field.travel import MapsProvider, OsrmProvider, TravelMatrix, TravelUnavailable, bucket

WEEKDAY_9 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)      # a Monday
WEEKDAY_22 = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)
SATURDAY_9 = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
A, B, C = (12.97, 77.59), (12.93, 77.62), (13.00, 77.55)


class Road:
    """A road provider with a one-way street: A to B is quick, B to A takes the long way round."""
    name = "maps"

    def __init__(self, times=None, fail=False):
        self.times, self.fail, self.calls = times or {}, fail, []

    def minutes(self, o, d, departure):
        self.calls.append((o, d, bucket(departure)))
        if self.fail:
            raise TimeoutError("down")
        base = self.times.get((o, d), 10.0)
        return base * (1.5 if departure.hour in (8, 9, 17, 18) else 1.0)


# ================================================================== #106
def test_the_matrix_is_not_assumed_symmetric(conn):
    road = Road({(A, B): 10.0, (B, A): 24.2, (A, C): 6.0, (C, A): 6.0, (B, C): 8.0, (C, B): 30.0})
    m = TravelMatrix(conn, [road]).matrix([A, B, C], WEEKDAY_22)
    assert m == [[0, 10, 6], [25, 0, 8], [6, 30, 0]] and m[0][1] != m[1][0]            # 24.2 minutes rounds up to 25


def test_repeats_are_cache_hits_and_the_departure_bucket_is_part_of_the_key(conn):
    road = Road()
    tm = TravelMatrix(conn, [road])
    tm.minutes(A, B, WEEKDAY_22)
    tm.minutes(A, B, WEEKDAY_22.replace(minute=30))                                    # same hour: cached
    assert len(road.calls) == 1
    rush = tm.minutes(A, B, WEEKDAY_9)
    assert len(road.calls) == 2 and rush == 15 and tm.minutes(A, B, WEEKDAY_22) == 10          # rush hour costs more than night
    tm.minutes(A, B, SATURDAY_9)                                                       # weekend 09:00 is its own bucket
    assert len(road.calls) == 3 and bucket(WEEKDAY_9) == "wd-09" and bucket(SATURDAY_9) == "we-09"
    TravelMatrix(conn, [road]).minutes(A, B, WEEKDAY_9)                                # a new object, same database: still cached
    assert len(road.calls) == 3


def test_the_same_place_is_zero_minutes_without_asking_anyone(conn):
    road = Road()
    assert TravelMatrix(conn, [road]).minutes(A, (12.970001, 77.590001), WEEKDAY_9) == 0 and road.calls == []


def test_a_failing_primary_falls_back_and_all_failing_raises_never_a_straight_line(conn):
    primary, fallback = Road(fail=True), Road({(A, B): 12.0})
    fallback.name = "osrm"
    tm = TravelMatrix(conn, [primary, fallback])
    assert tm.minutes(A, B, WEEKDAY_22) == 12 and len(primary.calls) == 1
    assert conn.execute("SELECT provider FROM travel_cache").fetchone()[0] == "osrm"
    dead = TravelMatrix(conn, [Road(fail=True)])
    with pytest.raises(TravelUnavailable) as e:
        dead.minutes(A, C, WEEKDAY_22)
    assert "maps: TimeoutError" in str(e.value)
    with pytest.raises(ValueError):
        TravelMatrix(conn, [])
    src = (Path(__file__).resolve().parent.parent / "field" / "travel.py").read_text(encoding="utf-8").lower()
    assert "haversine" not in src and "great_circle" not in src and "hypot" not in src


def test_osrm_and_maps_providers_parse_their_responses():
    seen = []

    def osrm(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"code": "Ok", "routes": [{"duration": 600.0}]})

    p = OsrmProvider(httpx.Client(transport=httpx.MockTransport(osrm), base_url="http://osrm.invalid"))
    assert p.minutes(A, B, WEEKDAY_9) == 10.0 and "/route/v1/driving/77.59,12.97;77.62,12.93" in seen[0]          # OSRM wants lng,lat
    bad = OsrmProvider(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "NoRoute"})), base_url="http://osrm.invalid"))
    with pytest.raises(TravelUnavailable):
        bad.minutes(A, B, WEEKDAY_9)

    def maps(req):
        assert req.url.params["departure_time"] and req.url.params["key"] == "k"
        return httpx.Response(200, json={"rows": [{"elements": [{"status": "OK", "duration": {"value": 600}, "duration_in_traffic": {"value": 900}}]}]})

    assert MapsProvider(httpx.Client(transport=httpx.MockTransport(maps), base_url="http://maps.invalid"), "k").minutes(A, B, WEEKDAY_9) == 15.0
    nope = MapsProvider(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"rows": [{"elements": [{"status": "ZERO_RESULTS"}]}]})), base_url="http://m.invalid"), "k")
    with pytest.raises(TravelUnavailable):
        nope.minutes(A, B, WEEKDAY_9)


# ================================================================== #107
def grid_matrix(points, uphill=1):
    """Manhattan minutes plus a one-way penalty for moving 'up' (a stand-in for a one-way street): asymmetric on purpose."""
    return [[0 if a == b else abs(a[0] - b[0]) + abs(a[1] - b[1]) + (uphill if b[1] > a[1] else 0) for b in points] for a in points]


def random_instance(seed, n, window=True):
    rng = random.Random(seed)
    pts = [(rng.randint(0, 40), rng.randint(0, 40)) for _ in range(n + 1)]
    m = grid_matrix(pts)
    stops = []
    for i in range(1, n + 1):
        lo = 540 + rng.randint(0, 120) if window else 0
        stops.append(Stop(f"S{i}", i, value=rng.randint(1, 10), service=rng.choice([10, 15, 20]), earliest=lo, latest=lo + rng.randint(120, 300) if window else HORIZON))
    return m, stops, Vehicle("V1", 0, shift_start=540, shift_end=1020)


def check_plan(plan: Plan, stops, vehicles, matrix):
    """An independent validator: windows, sequential travel, shift limits, no repeats, value accounting."""
    byid = {s.id: s for s in stops}
    seen = []
    for v in vehicles:
        t, node = v.shift_start, v.start
        for visit in plan.routes[v.id]:
            s = byid[visit.stop_id]
            assert visit.arrive >= t + matrix[node][s.node], (v.id, visit)
            assert visit.arrive >= s.earliest and (visit.arrive <= s.latest or visit.stop_id in plan.late), visit
            assert visit.leave == visit.arrive + s.service
            t, node = visit.leave, s.node
            seen.append(visit.stop_id)
        assert t + matrix[node][v.start if v.end is None else v.end] <= v.shift_end
    assert len(seen) == len(set(seen)) and set(seen) | set(plan.skipped) == set(byid) and not set(seen) & set(plan.skipped)
    assert plan.collected == sum(byid[i].value for i in seen)


def brute_force(matrix, stops, vehicle):
    best = 0
    for r in range(len(stops) + 1):
        for combo in combinations(range(len(stops)), r):
            for perm in permutations(combo):
                t, node, ok = vehicle.shift_start, vehicle.start, True
                for j in perm:
                    s = stops[j]
                    arrive = max(t + matrix[node][s.node], s.earliest)
                    if arrive > s.latest:
                        ok = False
                        break
                    t, node = arrive + s.service, s.node
                if ok and t + matrix[node][vehicle.start] <= vehicle.shift_end:
                    best = max(best, sum(stops[j].value for j in perm))
    return best


def test_the_exact_solver_agrees_with_brute_force_on_random_instances():
    for seed in range(25):
        m, stops, v = random_instance(seed, 5)
        plan = solve_exact(m, stops, v)
        assert plan.collected == brute_force(m, stops, v), seed
        check_plan(plan, stops, [v], m)


def test_gate_g4_the_heuristic_matches_the_exact_optimum_on_the_demo_instance():
    m, stops, v = random_instance(7, 9)
    exact = solve_exact(m, stops, v)
    fast = solve_routes(m, stops, [v], time_limit_s=2.0)
    check_plan(exact, stops, [v], m)
    check_plan(fast, stops, [v], m)
    assert exact.collected > 0 and fast.collected == exact.collected, (exact.collected, fast.collected)


def test_the_heuristic_never_beats_the_exact_value_and_stays_close():
    gap = []
    for seed in range(10):
        m, stops, v = random_instance(100 + seed, 7)
        exact, fast = solve_exact(m, stops, v), solve_routes(m, stops, [v], time_limit_s=0.5)
        check_plan(fast, stops, [v], m)
        assert fast.collected <= exact.collected, seed                                   # if it did, the exact solver or the validator is wrong
        gap.append(fast.collected / exact.collected if exact.collected else 1.0)
    assert sum(gap) / len(gap) >= 0.95, gap


def test_planning_everything_together_beats_taking_the_nearest_lead_first():
    # node 1 is close and worth 1; node 2 is far and worth 10. Taking the nearest first leaves no time for the valuable one.
    m = [[0, 5, 60], [5, 0, 70], [60, 70, 0]]
    stops = [Stop("near", 1, value=1, service=20), Stop("far", 2, value=10, service=20)]
    v = Vehicle("V1", 0, shift_start=540, shift_end=540 + 160)       # far alone: 60 + 20 + 60 = 140 fits; near then far: 5 + 20 + 70 + 20 + 60 = 175 does not
    nearest_first = 1                                                                   # what a one-lead-at-a-time assignment collects
    for plan in (solve_exact(m, stops, v), solve_routes(m, stops, [v], time_limit_s=0.5)):
        assert plan.collected == 10 > nearest_first and plan.skipped == ["near"]


def test_time_windows_service_times_and_shift_end_are_respected_with_waiting_allowed():
    m = [[0, 10, 10], [10, 0, 10], [10, 10, 0]]
    stops = [Stop("late_window", 1, value=5, service=30, earliest=700, latest=760), Stop("any", 2, value=3, service=30)]
    v = Vehicle("V1", 0, shift_start=540, shift_end=900)
    plan = solve_routes(m, stops, [v], time_limit_s=0.5)
    check_plan(plan, stops, [v], m)
    arrive = {x.stop_id: x.arrive for x in plan.routes["V1"]}
    assert arrive["late_window"] >= 700 and plan.collected == 8                           # it waits for the window rather than skipping it


def test_two_people_split_the_stops_and_nobody_is_visited_twice():
    m, stops, _ = random_instance(11, 8)
    vs = [Vehicle("V1", 0, shift_start=540, shift_end=1020), Vehicle("V2", 0, shift_start=540, shift_end=1020)]
    plan = solve_routes(m, stops, vs, time_limit_s=1.0)
    check_plan(plan, stops, vs, m)
    one = solve_routes(m, stops, vs[:1], time_limit_s=1.0)
    assert plan.collected >= one.collected and len(plan.routes) == 2


def test_service_mode_visits_every_stop_and_meets_feasible_deadlines():
    m = [[0, 10, 12, 15], [10, 0, 8, 9], [12, 8, 0, 7], [15, 9, 7, 0]]
    stops = [Stop("a", 1, service=20, latest=700), Stop("b", 2, service=20, latest=800), Stop("c", 3, service=20, latest=900)]
    v = Vehicle("T1", 0, shift_start=540, shift_end=1020)
    plan = solve_routes(m, stops, [v], mode="service", time_limit_s=0.5)
    check_plan(plan, stops, [v], m)
    assert plan.feasible and plan.late == {} and plan.skipped == [] and sorted(plan.visited()) == ["a", "b", "c"]


def test_service_mode_that_cannot_meet_the_deadlines_still_visits_everyone_and_names_the_breaches():
    m = [[0, 30, 30], [30, 0, 30], [30, 30, 0]]
    stops = [Stop("a", 1, service=30, latest=580), Stop("b", 2, service=30, latest=585)]      # both due within 45 minutes of a 540 start; impossible for one person
    v = Vehicle("T1", 0, shift_start=540, shift_end=1020)
    plan = solve_routes(m, stops, [v], mode="service", time_limit_s=0.5)
    assert plan.feasible is False and plan.skipped == [] and sorted(plan.visited()) == ["a", "b"]
    assert plan.late and all(minutes > 0 for minutes in plan.late.values())
    check_plan(plan, stops, [v], m)


def test_replan_after_a_closed_shop_is_fast_starts_from_where_the_rep_is_and_skips_done_visits():
    m, stops, v = random_instance(7, 9)
    before = solve_routes(m, stops, [v], time_limit_s=1.0)
    first, second = before.routes["V1"][0], before.routes["V1"][1]
    closed = second.stop_id
    node_of = {s.id: s.node for s in stops}
    after = replan(m, stops, [v], closed=[closed], done=[first.stop_id], now=first.leave, positions={"V1": node_of[first.stop_id]}, time_limit_s=1.0)
    assert closed not in after.visited() and first.stop_id not in after.visited() and closed not in after.skipped
    assert all(x.arrive >= first.leave for x in after.routes["V1"])
    remaining = [s for s in stops if s.id not in (closed, first.stop_id)]
    moved = Vehicle("V1", node_of[first.stop_id], shift_start=first.leave, shift_end=v.shift_end)
    check_plan(after, remaining, [moved], m)


def test_an_unknown_mode_is_an_error():
    m, stops, v = random_instance(1, 3)
    with pytest.raises(ValueError):
        solve_routes(m, stops, [v], mode="fastest")
    with pytest.raises(ValueError):
        solve_exact(m, [Stop(f"S{i}", 1) for i in range(13)], v)


def test_replan_still_sends_the_rep_home_to_the_original_end_not_to_where_they_stand():
    # Depot 0 is far from the rep's current spot (node 1). Stop X (node 2) is 5 from the rep but 50 from the depot.
    m = [[0, 50, 50], [50, 0, 5], [50, 5, 0]]
    x = Stop("X", 2, value=5, service=10)
    home = Vehicle("V1", 0, shift_start=540, shift_end=540 + 60)
    # From node 1: 5 to X + 10 service + 50 home = 65 minutes > 60, so X cannot be visited and still get home (it could if the rep ended at node 1).
    plan = replan(m, [x], [home], now=540, positions={"V1": 1}, time_limit_s=0.5)
    assert plan.visited() == [] and plan.skipped == ["X"]
    # With a shift long enough for the way home, X is visited and the return leg is counted from X to the depot.
    roomy = Vehicle("V1", 0, shift_start=540, shift_end=540 + 65)
    plan = replan(m, [x], [roomy], now=540, positions={"V1": 1}, time_limit_s=0.5)
    assert plan.visited() == ["X"] and plan.travel == 5 + 50
