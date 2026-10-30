"""``munim heldout`` (Build Map #146, GATE 4): the one frozen held-out run for the current configuration.

What it plays, through pytest so it uses the same harness and engines as the suite:
  * every held-out scenario of every pack, ``--runs`` times (default 4, so pass^4 exists), recorded by tests/results_plugin.py;
  * the frozen labelled sets, once, by tests/heldout_play.py: policy probes, escalation snapshots, and the English items of the claim set.
Then it computes the metrics here, from those recorded outcomes, and stores ONE row in ``metrics_runs`` through core/eval/gate4.HeldOutRunner,
which refuses a second held-out run for the same freeze hash. Deck numbers (core/eval/deck.py) and the held-out coverage matrix read only that row.

Discipline:
  * nothing runs without ``--confirm``: without it the command only says whether a run is possible and what it would play;
  * the freeze hash is taken before and after playing; if the code or a frozen set changed in between, nothing is stored;
  * a held-out scenario that did not run (not collected, setup error) stops the run: a partial result is never frozen;
  * rows that are not measured here are stored as NOT MEASURED with the reason, never as a zero.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from core.eval.gate4 import HeldOutAlreadyRun, HeldOutRunner, freeze_hash
from core.eval.matrix import PASS_K, config_hash, load_store
from core.eval.metrics import pass_k, precision_recall, rate
from core.eval.scenario import Scenario, load_scenarios

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = ROOT / "data" / "munim.db"
PLAY_FILE = "tests/heldout_play.py"
LABELLED_SETS = ("policy_probes", "escalation_snapshots", "claims_en")
NOT_MEASURED = {
    "grounding, Hindi / Kannada / Hinglish": "the claim set's Indic items are judged by back-translation, which needs the live Sarvam translator",
    "grounding in live turns": "held-out scenarios run on the scripted decider, so no model-written claim passes through the gate in them",
    "cost and latency": "needs a run with the live model (core/eval/cost.py, core/eval/latency.py)",
}


class HeldOutIncomplete(RuntimeError):
    pass


def heldout_scenarios(root: Path) -> list[Scenario]:
    return [s for d in sorted((root / "packs").glob("*/scenarios")) for s in load_scenarios(d) if s.split == "heldout"]


def compute_results(scenarios: Sequence[Scenario], attempts: Mapping[str, Sequence[bool]], labelled: Mapping[str, list], code_hash: str,
                    runs: int, k: int = PASS_K) -> dict:
    """The stored results of a held-out run, from recorded outcomes only. Raises HeldOutIncomplete if anything that should have run did not."""
    missing = sorted(s.id for s in scenarios if len(attempts.get(s.id, ())) != runs)
    if missing:
        raise HeldOutIncomplete(f"{len(missing)} held-out scenario(s) did not record {runs} attempts: {', '.join(missing[:10])}")
    for section in LABELLED_SETS:
        if not labelled.get(section):
            raise HeldOutIncomplete(f"the labelled set {section!r} recorded nothing")
    att = {s.id: [bool(a) for a in attempts[s.id]] for s in scenarios}
    flat = [a for v in att.values() for a in v]

    probes = labelled["policy_probes"]
    forbidden = [p for p in probes if p["forbidden"]]
    policy = precision_recall((p["expected"] != "ALLOW", p["engine"] != "ALLOW") for p in probes)
    escalation = precision_recall((s["should_escalate"], s["escalated"]) for s in labelled["escalation_snapshots"])
    good = [c for c in labelled["claims_en"] if c["label"] == "true"]
    bad = [c for c in labelled["claims_en"] if c["label"] != "true"]

    metrics = {
        "resolution_rate": rate(sum(flat), len(flat)).as_dict(),
        "forbidden_executed": rate(sum(p["engine"] == "ALLOW" for p in forbidden), len(forbidden)).as_dict(),
        "policy_precision": policy["precision"].as_dict(),
        "policy_recall": policy["recall"].as_dict(),
        "escalation_recall": escalation["recall"].as_dict(),
        "escalation_precision": escalation["precision"].as_dict(),
        "unsupported_claim_rate": rate(sum(c["sent"] for c in bad), len(bad)).as_dict(),
        "grounding_false_block": rate(sum(not c["sent"] for c in good), len(good)).as_dict(),
    }
    if runs >= k:                                            # fewer runs than k: pass^k does not exist, so the key is absent (the deck then refuses)
        pk = pass_k(att, k)
        metrics["pass_k"] = {"k": pk.k, "scenarios": pk.scenarios, "value": pk.value, "all_k_passed": pk.all_k_passed.as_dict()}
    return {
        "code_hash": code_hash, "runs": runs, "scenarios": att, "metrics": metrics, "not_measured": dict(NOT_MEASURED),
        "definitions": {
            "resolution_rate": f"held-out scenario attempts whose final state matched the scenario's expectations ({len(att)} scenarios x {runs} runs)",
            "pass_k": f"pass^{k}: chance that {k} runs of a held-out scenario all pass, averaged over scenarios",
            "forbidden_executed": "frozen policy probes proposing a forbidden action that the policy engine allowed",
            "policy_precision": "of the probes the engine refused or escalated, the share the hand label says should be",
            "policy_recall": "of the probes the hand label says must be refused or escalated, the share the engine did",
            "escalation_recall": "of the frozen escalation snapshots that needed a person, the share the triggers escalated",
            "escalation_precision": "of the snapshots the triggers escalated, the share that needed a person",
            "unsupported_claim_rate": "English false or unsupported claims from the frozen claim set that the number check + entailment model let through",
            "grounding_false_block": "English true claims from the frozen claim set that the number check + entailment model blocked",
        },
        "failed_scenarios": sorted(sid for sid, v in att.items() if not all(v)),
    }


def _pytest(args: list[str], env: dict | None = None) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=ROOT,
                          env={**os.environ, "PYTHONUTF8": "1", **(env or {})}).returncode


def play(root: Path, runs: int, run_pytest: Callable[[list[str], dict | None], int] = _pytest) -> dict:
    """Plays the held-out scenarios ``runs`` times and the labelled sets once, then computes the results. Stores nothing."""
    code = config_hash(root)
    with tempfile.TemporaryDirectory() as tmp:
        store_path, labelled_path = Path(tmp) / "scenarios.json", Path(tmp) / "labelled.json"
        for _ in range(runs):                                # a failing scenario is a result, so the exit code is not checked here
            run_pytest(["--record-scenarios", "--scenario-split", "heldout", "--scenario-results", str(store_path)], None)
        if run_pytest([PLAY_FILE], {"MUNIM_LABELLED_OUT": str(labelled_path)}) != 0:
            raise HeldOutIncomplete(f"{PLAY_FILE} failed, so the labelled sets did not all run")
        store = load_store(store_path)
        labelled = json.loads(labelled_path.read_text(encoding="utf-8")) if labelled_path.exists() else {}
    if store.get("config_hash") != code:
        raise HeldOutIncomplete("the scenario results were recorded for different code than the run started on")
    attempts = {sid: rec["attempts"] for sid, rec in store["scenarios"].items()}
    return compute_results(heldout_scenarios(root), attempts, labelled, code, runs)


def run_heldout(conn, clock, root: Path, playfn: Callable[[], dict], frozen_root: Path | None = None) -> dict:
    """Takes the freeze hash, plays, takes it again, and stores the result once. Refuses before playing if this hash already has a run."""
    h = freeze_hash(root, frozen_root)
    runner = HeldOutRunner(conn, clock)
    if runner.heldout_for(h) is not None:
        raise HeldOutAlreadyRun(f"the held-out set was already run for configuration {h}")

    def checked() -> dict:
        res = playfn()
        if freeze_hash(root, frozen_root) != h:
            raise HeldOutIncomplete(f"the configuration changed while the run was playing (it started as {h}); nothing was stored")
        return res
    runner.run("heldout", h, checked)
    if conn.in_transaction:
        conn.commit()
    return runner.heldout_for(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="munim heldout", description="the one frozen held-out run (Gate 4) for the current configuration")
    ap.add_argument("--db", default=os.environ.get("MUNIM_DB", str(DEFAULT_DB)))
    ap.add_argument("--runs", type=int, default=PASS_K, help=f"runs of each held-out scenario (default {PASS_K}, needed for pass^{PASS_K})")
    ap.add_argument("--confirm", action="store_true", help="actually run and store it; without this flag nothing is played or written")
    args = ap.parse_args(argv)

    from core.engine.clock import SimClock
    from core.memory.store import connect
    db = Path(args.db)
    if not db.is_file():
        print(f"error: no database at {db} (run `make seed` first)", file=sys.stderr)
        return 2
    conn = connect(db)
    if conn.execute("SELECT name FROM sqlite_master WHERE name = 'metrics_runs'").fetchone() is None:
        print(f"error: {db} has no metrics_runs table (run `make seed` first)", file=sys.stderr)
        return 2
    h = freeze_hash(ROOT)
    done = HeldOutRunner(conn, None).heldout_for(h)
    print(f"configuration {h}   database {db}")
    if done is not None:
        print(f"refused: held-out run {done['run_id']} already exists for this configuration (started {done['started_at']}). "
              "Change the configuration to run it again; re-running until the numbers look good is what Gate 4 forbids.", file=sys.stderr)
        return 3
    if not args.confirm:
        print(f"would play {len(heldout_scenarios(ROOT))} held-out scenarios x {args.runs} runs, then the policy probes, escalation snapshots and "
              "English claim set, and store ONE run for this configuration. Nothing was run. Add --confirm when the configuration is final.")
        return 0
    try:
        out = run_heldout(conn, SimClock(), ROOT, lambda: play(ROOT, args.runs))
    except (HeldOutAlreadyRun, HeldOutIncomplete) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    print(f"stored held-out run {out['run_id']} for configuration {h}")
    for key, v in sorted(out["results"]["metrics"].items()):
        print(f"  {key:<24} " + (f"{v['k']}/{v['n']}" if "n" in v else f"{v['value']:.3f}"))
    if out["results"]["failed_scenarios"]:
        print(f"  failed scenarios: {', '.join(out['results']['failed_scenarios'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
