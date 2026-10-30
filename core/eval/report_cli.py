"""``munim eval --split dev`` (Build Map #142): all three metric tiers with 95% Wilson intervals, from RECORDED scenario results.

The command runs the dev scenarios through pytest with the results plugin (tests/results_plugin.py), which records one pass/fail attempt per scenario against the
hash of the code it ran on, then prints the tiers from that store. It refuses stale results (the code changed since), and it never runs the held-out split: that is
run once, by Gate 4 (core/eval/gate4.py), and its numbers come only from there.

Rows this command cannot compute from pass/fail alone are printed as NOT MEASURED HERE with where they are measured, never as a zero or a guess.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from core.eval.matrix import config_hash, load_store
from core.eval.metrics import pass_k, rate
from core.eval.scenario import Scenario, load_scenarios

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RESULTS = ROOT / "eval" / "out" / "scenario_results.json"
SAFETY = ("adversarial", "should_escalate")
NOT_HERE = {
    "grounding catch / false-block": "tests/gate_grounding.py (Gate 3) and the frozen claim set",
    "policy precision / recall": "the frozen policy probes (eval/frozen/probes) and tests/test_assurance.py",
    "forbidden executed": "tests/gate_forbidden.py (Gate 2)",
    "tool-failure handling": "tests/test_chaos.py",
    "Scam Shield accuracy": "tests/test_scam_shield.py",
    "mean turns and touches": "not recorded per scenario yet",
}


def _fmt(r) -> str:
    if r.n == 0:
        return "n=0 (nothing measured)"
    return f"{r.k}/{r.n} = {r.value:.1%}  95% [{r.lo:.1%}, {r.hi:.1%}] ({r.method})"


def build_report(scenarios: list[Scenario], store: dict, split: str, current_hash: str, k: int = 4) -> dict:
    if split != "dev":
        raise ValueError("only the dev split can be evaluated here; the held-out split runs once, in Gate 4 (core/eval/gate4.py)")
    mine = [s for s in scenarios if s.split == split]
    stale = store.get("config_hash") != current_hash
    out = {"split": split, "config_hash": current_hash, "results_config_hash": store.get("config_hash") or None, "stale": stale,
           "last_run": store.get("last_run"), "scenarios": len(mine)}
    if stale:
        out["error"] = f"results are stale: recorded for config {out['results_config_hash'] or 'none'}, current config is {current_hash}; run without --no-run"
        return out
    latest, attempts, not_run = {}, {}, []
    for s in mine:
        rec = store["scenarios"].get(s.id)
        if not rec or not rec["attempts"]:
            not_run.append(s.id)
            continue
        latest[s.id] = bool(rec["attempts"][-1])
        attempts[s.id] = [bool(a) for a in rec["attempts"]]
    by_pb: dict[str, list[bool]] = {}
    for s in mine:
        if s.id in latest:
            by_pb.setdefault(s.playbook_id, []).append(latest[s.id])
    safety = [latest[s.id] for s in mine if s.family in SAFETY and s.id in latest]
    esc = [latest[s.id] for s in mine if s.family == "should_escalate" and s.id in latest]
    runs_ok = bool(attempts) and all(len(v) >= k for v in attempts.values())
    pk = pass_k(attempts, k) if runs_ok else None
    out.update({
        "not_run": not_run,
        "tier_a": {"safety scenarios passed (adversarial + should-escalate)": rate(sum(safety), len(safety)),
                   "should-escalate scenarios passed": rate(sum(esc), len(esc)),
                   "not_measured_here": dict(NOT_HERE)},
        "tier_b": {"scenarios passed (latest run)": rate(sum(latest.values()), len(latest)),
                   "by_playbook": {pb: rate(sum(v), len(v)) for pb, v in sorted(by_pb.items())},
                   "pass_k": pk, "k": k, "min_attempts": min((len(v) for v in attempts.values()), default=0)},
        "tier_c": _cost(),
    })
    return out


def _cost() -> dict:
    p = ROOT / "eval" / "out" / "cost_report.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"status": "NOT MEASURED HERE", "where": "core/eval/cost.py (needs a run with token counts)"}


def render(rep: dict) -> str:
    L = [f"munim eval --split {rep['split']}   config {rep['config_hash']}   scenarios {rep['scenarios']}"]
    if rep.get("error"):
        return "\n".join(L + ["ERROR: " + rep["error"]])
    lr = rep.get("last_run") or {}
    L.append(f"results: run {lr.get('run')} recorded {lr.get('recorded_at')}" + (f"   NOT RUN: {', '.join(rep['not_run'])}" if rep["not_run"] else ""))
    L.append("")
    L.append("TIER A  safety")
    for name, r in rep["tier_a"].items():
        if name == "not_measured_here":
            for n, where in r.items():
                L.append(f"  {n:<44} NOT MEASURED HERE: {where}")
        else:
            L.append(f"  {name:<44} {_fmt(r)}")
    b = rep["tier_b"]
    L += ["", "TIER B  does the job", f"  {'scenarios passed (latest run)':<44} {_fmt(b['scenarios passed (latest run)'])}"]
    for pb, r in b["by_playbook"].items():
        L.append(f"    {pb:<42} {_fmt(r)}")
    pk = b["pass_k"]
    if pk is None:
        L.append(f"  {'pass^' + str(b['k']):<44} NOT AVAILABLE: needs {b['k']} recorded runs of every scenario, have {b['min_attempts']}")
    else:
        L.append(f"  {'pass^' + str(pk.k):<44} {pk.value:.3f} over {pk.scenarios} scenarios; all {pk.k} runs passed: {_fmt(pk.all_k_passed)}")
    L += ["", "TIER C  cost"]
    c = rep["tier_c"]
    L.append("  " + (f"{c['status']}: {c['where']}" if "status" in c else json.dumps(c, sort_keys=True)))
    return "\n".join(L)


def run_dev_scenarios(results_path: Path) -> int:
    """pytest over the dev scenario tests only (never the held-out ones), recording one attempt each against the current code hash."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--record-scenarios", "--scenario-split", "dev", "--scenario-results", str(results_path)]
    return subprocess.run(cmd, cwd=ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="munim eval")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--pack", default="paytm_merchant")
    ap.add_argument("--results", default=str(DEFAULT_RESULTS))
    ap.add_argument("--no-run", action="store_true", help="print from the stored results without running the scenarios again")
    ap.add_argument("-k", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.split != "dev":
        print("error: only --split dev can be evaluated here. The held-out split runs once per configuration, in Gate 4 (core/eval/gate4.py).", file=sys.stderr)
        return 2
    results = Path(args.results)
    if not args.no_run:
        run_dev_scenarios(results)                       # a failing scenario is data, not a reason to stop the report
    rep = build_report(load_scenarios(ROOT / "packs" / args.pack / "scenarios"), load_store(results), args.split, config_hash(ROOT), args.k)
    if args.json:
        print(json.dumps(rep, indent=1, sort_keys=True, default=lambda o: o.as_dict() if hasattr(o, "as_dict") else o.__dict__))
    else:
        print(render(rep))
    return 1 if rep.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
