"""Guided tour of Judge Mode (#135). Steps are data (tour.yaml). Progress is computed from the ledger, per session, on every call.

There is no tour state anywhere: no table, no flag, no cache. A fresh persona session therefore starts with every step pending, and a step
turns done only when the ledger shows the fact it names. The 'nothing forbidden' step is an invariant that needs an attempt first, so an
empty session cannot show it as done.
"""
from __future__ import annotations

import json
import operator
import sqlite3
from pathlib import Path

import yaml

from core.judge import scoreboard as sb
from core.judge.session import ACTOR, OP_ATTEMPT, OP_FEEDBACK, JudgeSession

TOUR_PATH = Path(__file__).with_name("tour.yaml")
OPS = {">=": operator.ge, "==": operator.eq}
METRICS = frozenset({"session_started", "attempts", "blocked_claims", "refused_actions", "escalations", "forbidden_executed", "fine_marks"})
STEP_KEYS = {"id", "title", "ui_element", "ui_label", "instruction", "fact", "proves", "clip"}


class TourError(ValueError):
    pass


def _check_fact(f: dict, where: str) -> None:
    if set(f) - {"metric", "op", "value", "requires"}:
        raise TourError(f"{where}: unknown fact keys {sorted(set(f) - {'metric', 'op', 'value', 'requires'})}")
    if f.get("metric") not in METRICS or f.get("op") not in OPS or not isinstance(f.get("value"), int):
        raise TourError(f"{where}: bad fact {f}")
    if "requires" in f:
        _check_fact(f["requires"], where + ".requires")


def load_tour(path: Path = TOUR_PATH) -> list[dict]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    steps = doc["steps"]
    ids = [s.get("id") for s in steps]
    if len(ids) != len(set(ids)):
        raise TourError("duplicate step id")
    for s in steps:
        miss = {"id", "title", "ui_element", "instruction", "fact", "proves", "clip"} - set(s)
        if miss or set(s) - STEP_KEYS:
            raise TourError(f"{s.get('id')}: missing {sorted(miss)} / unknown {sorted(set(s) - STEP_KEYS)}")
        _check_fact(s["fact"], s["id"])
    return steps


def _value(conn: sqlite3.Connection, s: JudgeSession, board: dict, metric: str) -> int:
    return 1 if metric == "session_started" else int(board[metric])      # session_started: load_session found the start entry


def _first_seq(conn: sqlite3.Connection, s: JudgeSession, metric: str) -> int | None:
    """The ledger entry that first proves the metric, when one entry does. None for a count that has no single entry."""
    if metric == "session_started":
        return s.start_seq
    own = {"attempts": OP_ATTEMPT, "fine_marks": OP_FEEDBACK}.get(metric)
    if own:
        r = conn.execute("SELECT MIN(seq) FROM ledger WHERE seq > ? AND kind = 'HUMAN_ACTION' AND actor = ? AND json_extract(payload_json, '$.op') = ? "
                         "AND json_extract(payload_json, '$.session_id') = ?", (s.start_seq, ACTOR, own, s.session_id)).fetchone()
        return r[0]
    kind = {"blocked_claims": "VERIFY", "refused_actions": "POLICY", "escalations": "ESCALATION"}.get(metric)
    if kind is None:
        return None
    for seq, _, pj in conn.execute(sb._MERCHANT_ROWS, (s.start_seq, s.merchant_id, kind)).fetchall():
        p = json.loads(pj)
        if (kind == "VERIFY" and any(not c.get("sent") for c in p.get("claims") or [])) or (kind == "POLICY" and p.get("allow") is False) or kind == "ESCALATION":
            return seq
    return None


def progress(conn: sqlite3.Connection, s: JudgeSession, steps: list[dict] | None = None, board: dict | None = None) -> dict:
    """{steps: [{id, done, value, needed, op, evidence_seq}], done, total, next}. Read-only; everything comes from the ledger."""
    steps = steps if steps is not None else load_tour()
    board = board if board is not None else sb.compute(conn, s)
    out = []
    for st in steps:
        f = st["fact"]
        v = _value(conn, s, board, f["metric"])
        done = OPS[f["op"]](v, f["value"])
        if done and "requires" in f:
            r = f["requires"]
            done = OPS[r["op"]](_value(conn, s, board, r["metric"]), r["value"])
        out.append({"id": st["id"], "done": bool(done), "metric": f["metric"], "value": v, "op": f["op"], "needed": f["value"],
                    "evidence_seq": _first_seq(conn, s, f["metric"]) if done else None})
    nxt = next((x["id"] for x in out if not x["done"]), None)
    return {"steps": out, "done": sum(x["done"] for x in out), "total": len(out), "next": nxt, "label": "SIMULATED"}


def missing_clips(public_dir: Path, steps: list[dict] | None = None) -> list[str]:
    return [s["id"] for s in (steps or load_tour()) if not (Path(public_dir) / s["clip"]).is_file()]
