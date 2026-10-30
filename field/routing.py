"""Routing engine, shared by sales visits and service dispatch (#107).

All stops of all people are planned TOGETHER (assigning one lead at a time gives bad routes). Inputs are a minutes matrix (see travel.py), stops
with time windows and service times, and vehicles (a rep or technician) with a shift and start and end nodes. Times are minutes of the day.

* SALES mode: every stop is optional. Skipping a stop costs its value (a Team Orienteering problem with time windows): the plan collects as
  much value as the shift allows, then travels as little as it can.
* SERVICE mode: every stop is mandatory and its ``latest`` is an SLA deadline. If the deadlines cannot all be met the plan is still complete,
  ``feasible`` is False and ``late`` names the breached stops with how late they are. Nothing is silently dropped.
* ``solve_exact`` is an exact subset DP for ONE vehicle and up to 12 optional stops. It is the oracle that the heuristic is checked against
  (gate G4); ``solve_routes`` (OR-Tools, guided local search) is what runs on real instances.
* ``replan`` re-solves after a closure or cancellation from where each person is now, in seconds.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import combinations
from typing import Sequence

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

HORIZON = 24 * 60
VALUE_SCALE = 1000          # a skipped stop costs value * VALUE_SCALE, a minute of travel costs 1: value always beats travel


@dataclass(frozen=True)
class Stop:
    id: str
    node: int                 # row/column in the matrix
    value: int = 1            # what visiting it is worth (integer; paise of expected value, or any consistent unit)
    service: int = 20         # minutes spent there
    earliest: int = 0         # service may start no earlier than this
    latest: int = HORIZON     # ... and no later than this (an SLA deadline in service mode)


@dataclass(frozen=True)
class Vehicle:
    id: str
    start: int                # matrix node where the person starts
    end: int | None = None    # where they must finish (default: back at the start)
    shift_start: int = 0
    shift_end: int = HORIZON


@dataclass(frozen=True)
class Visit:
    stop_id: str
    arrive: int
    leave: int


@dataclass
class Plan:
    routes: dict[str, list[Visit]]
    collected: int
    skipped: list[str]
    travel: int
    feasible: bool = True
    late: dict[str, int] = field(default_factory=dict)        # stop id -> minutes past its deadline (service mode, infeasible)
    solver: str = ""

    def visited(self) -> list[str]:
        return [v.stop_id for r in self.routes.values() for v in r]


# ------------------------------------------------------------------------------------------------------ exact (one vehicle)
def solve_exact(matrix: Sequence[Sequence[int]], stops: Sequence[Stop], vehicle: Vehicle) -> Plan:
    """Maximise collected value, then minimise finish time, for one vehicle over optional stops. Exponential: at most 12 stops."""
    n = len(stops)
    if n > 12:
        raise ValueError("solve_exact is for small instances (at most 12 stops)")
    end_node = vehicle.start if vehicle.end is None else vehicle.end
    INF = 10**9
    # best[mask][last] = (earliest time the service at `last` finishes, previous last) having visited `mask`
    best: dict[tuple[int, int], tuple[int, int]] = {}
    for j, s in enumerate(stops):
        arrive = max(vehicle.shift_start + matrix[vehicle.start][s.node], s.earliest)
        if arrive <= s.latest and arrive + s.service + matrix[s.node][end_node] <= vehicle.shift_end:
            best[(1 << j, j)] = (arrive + s.service, -1)
    for size in range(1, n):
        for combo in combinations(range(n), size):
            mask = sum(1 << i for i in combo)
            for last in combo:
                cur = best.get((mask, last))
                if cur is None:
                    continue
                for j in range(n):
                    if mask & (1 << j):
                        continue
                    s = stops[j]
                    arrive = max(cur[0] + matrix[stops[last].node][s.node], s.earliest)
                    if arrive > s.latest or arrive + s.service + matrix[s.node][end_node] > vehicle.shift_end:
                        continue
                    key = (mask | (1 << j), j)
                    fin = arrive + s.service
                    if key not in best or fin < best[key][0]:
                        best[key] = (fin, last)
    top: tuple[int, int, int, int] | None = None                  # (value, -finish, mask, last)
    for (mask, last), (fin, _) in best.items():
        v = sum(stops[i].value for i in range(n) if mask & (1 << i))
        cand = (v, -(fin + matrix[stops[last].node][end_node]), mask, last)
        if top is None or cand[:2] > top[:2]:
            top = cand
    if top is None:
        return Plan({vehicle.id: []}, 0, [s.id for s in stops], 0, solver="exact")
    _, _, mask, last = top
    order: list[int] = []
    while last != -1:
        order.append(last)
        mask, last = mask ^ (1 << last), best[(mask, last)][1]
    order.reverse()
    return _plan_from_order(matrix, stops, vehicle, order, "exact")


def _plan_from_order(matrix, stops, vehicle, order: list[int], solver: str) -> Plan:
    end_node = vehicle.start if vehicle.end is None else vehicle.end
    t, node, travel, visits = vehicle.shift_start, vehicle.start, 0, []
    for j in order:
        s = stops[j]
        travel += matrix[node][s.node]
        arrive = max(t + matrix[node][s.node], s.earliest)
        visits.append(Visit(s.id, arrive, arrive + s.service))
        t, node = arrive + s.service, s.node
    travel += matrix[node][end_node]
    got = {stops[j].id for j in order}
    return Plan({vehicle.id: visits}, sum(stops[j].value for j in order), [s.id for s in stops if s.id not in got], travel, solver=solver)


# ------------------------------------------------------------------------------------------------------ OR-Tools (many vehicles)
def solve_routes(matrix: Sequence[Sequence[int]], stops: Sequence[Stop], vehicles: Sequence[Vehicle], *, mode: str = "sales", time_limit_s: float = 2.0) -> Plan:
    if mode not in ("sales", "service"):
        raise ValueError("mode is 'sales' or 'service'")
    if mode == "sales":
        plan = _solve(matrix, stops, vehicles, optional=True, soft_deadlines=False, limit=time_limit_s)
        return plan if plan is not None else Plan({v.id: [] for v in vehicles}, 0, [s.id for s in stops], 0, solver="ortools")
    hard = _solve(matrix, stops, vehicles, optional=False, soft_deadlines=False, limit=time_limit_s)
    if hard is not None and hard.visited():
        return hard
    soft = _solve(matrix, stops, vehicles, optional=False, soft_deadlines=True, limit=time_limit_s)
    if soft is None:
        return Plan({v.id: [] for v in vehicles}, 0, [s.id for s in stops], 0, feasible=False, late={s.id: 0 for s in stops}, solver="ortools")
    return soft


def _solve(matrix, stops, vehicles, *, optional: bool, soft_deadlines: bool, limit: float) -> Plan | None:
    n_nodes = len(matrix)
    starts = [v.start for v in vehicles]
    ends = [v.start if v.end is None else v.end for v in vehicles]
    manager = pywrapcp.RoutingIndexManager(n_nodes, len(vehicles), starts, ends)
    routing = pywrapcp.RoutingModel(manager)
    service = [0] * n_nodes
    by_node = {s.node: s for s in stops}
    for s in stops:
        service[s.node] = s.service

    def transit(i, j):
        a, b = manager.IndexToNode(i), manager.IndexToNode(j)
        return int(matrix[a][b]) + service[a]

    cb = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(cb)
    routing.AddDimension(cb, HORIZON, HORIZON, False, "Time")
    time = routing.GetDimensionOrDie("Time")
    for s in stops:
        idx = manager.NodeToIndex(s.node)
        time.CumulVar(idx).SetRange(s.earliest, HORIZON if soft_deadlines else s.latest)
        if soft_deadlines:
            time.SetCumulVarSoftUpperBound(idx, s.latest, 100_000)
        if optional:
            routing.AddDisjunction([idx], s.value * VALUE_SCALE)
    for node in range(n_nodes):                         # OR-Tools makes every node mandatory: a node that is not a stop (a closed shop) must be switched off
        if node not in by_node and node not in starts and node not in ends:
            routing.solver().Add(routing.ActiveVar(manager.NodeToIndex(node)) == 0)
    for k, v in enumerate(vehicles):
        time.CumulVar(routing.Start(k)).SetRange(v.shift_start, v.shift_end)
        time.CumulVar(routing.End(k)).SetRange(v.shift_start, v.shift_end)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(limit * 1000))
    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None
    routes: dict[str, list[Visit]] = {}
    travel, late, visited = 0, {}, set()
    for k, v in enumerate(vehicles):
        idx, out, prev_node = routing.Start(k), [], v.start
        idx = sol.Value(routing.NextVar(idx))
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            s = by_node[node]
            arrive = sol.Min(time.CumulVar(idx))
            out.append(Visit(s.id, arrive, arrive + s.service))
            visited.add(s.id)
            if arrive > s.latest:
                late[s.id] = arrive - s.latest
            travel += matrix[prev_node][node]
            prev_node = node
            idx = sol.Value(routing.NextVar(idx))
        travel += matrix[prev_node][ends[k]]
        routes[v.id] = out
    value = sum(s.value for s in stops if s.id in visited)
    return Plan(routes, value, [s.id for s in stops if s.id not in visited], travel, feasible=not late, late=late, solver="ortools")


# ------------------------------------------------------------------------------------------------------ live re-planning
def replan(matrix: Sequence[Sequence[int]], stops: Sequence[Stop], vehicles: Sequence[Vehicle], *, closed: Sequence[str] = (), done: Sequence[str] = (),
           now: int, positions: dict[str, int] | None = None, mode: str = "sales", time_limit_s: float = 2.0) -> Plan:
    """A shop closed or a visit was cancelled: drop it (and what is already done), start every person from where they are, no earlier than ``now``."""
    gone = set(closed) | set(done)
    remaining = [s for s in stops if s.id not in gone]
    moved = [replace(v, start=(positions or {}).get(v.id, v.start), end=v.start if v.end is None else v.end, shift_start=max(v.shift_start, now)) for v in vehicles]
    return solve_routes(matrix, remaining, moved, mode=mode, time_limit_s=time_limit_s)
