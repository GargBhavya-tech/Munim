"""Coverage matrix and "Kyun nahi?" (Build Map #69). Everything is computed from recorded test results and rule metadata."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from core.api.deps import read_db
from core.eval.gate4 import matrix_store_from_run
from core.eval.matrix import compute_matrix, config_hash, explain_refusal, load_store
from core.eval.scenario import load_scenarios

router = APIRouter(prefix="/api")


def _rules(request: Request):
    rules = request.app.state.rules
    if rules is None:
        from packs.paytm_merchant.policy.rules import default_rules      # the one pack shipped today
        rules = default_rules()
    return rules


@router.get("/coverage")
def coverage(request: Request, source: str = "tests", conn=Depends(read_db)):
    """source=tests: recorded test results (#69). source=heldout: the stored Gate 4 held-out run (#149), stale if the code moved on since."""
    st = request.app.state
    if source not in ("tests", "heldout"):
        raise HTTPException(400, "source is 'tests' or 'heldout'")
    pack = Path(st.pack_dir)
    scenarios = load_scenarios(pack / "scenarios")
    halves, ids = {}, []
    for p in sorted((pack / "playbooks").glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        halves[d["id"]] = d.get("half")
        ids.append(d["id"])
    if source == "heldout":
        row = conn.execute("SELECT * FROM metrics_runs WHERE split = 'heldout' ORDER BY run_id DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(404, "no held-out run has been recorded yet")
        run = {"run_id": row["run_id"], "config_hash": row["config_hash"], "started_at": row["started_at"], "results": json.loads(row["results_json"])}
        store = matrix_store_from_run(run)
    else:
        store = load_store(st.results_path)
    return compute_matrix(scenarios, store, config_hash(Path(st.root)), halves, ids)


@router.get("/why/{seq}")
def why(seq: int, request: Request, conn=Depends(read_db)):
    """Kyun nahi? Explain a refused action from the POLICY ledger entry and the rule metadata."""
    r = conn.execute("SELECT kind, payload_json FROM ledger WHERE seq = ?", (seq,)).fetchone()
    if r is None:
        raise HTTPException(404, f"no ledger entry {seq}")
    payload = json.loads(r["payload_json"])
    if r["kind"] != "POLICY" or payload.get("allow") is not False:
        raise HTTPException(409, f"ledger entry {seq} is not a refusal")
    return {"seq": seq, **explain_refusal(payload, _rules(request))}
