"""GATE 4 machinery (#146): the frozen held-out run, and numbers for the deck that come from that run only.

* ``freeze_hash`` hashes everything a result depends on: the code and pack (core/eval/matrix.config_hash: prompts, policy, weights' names, thresholds, playbooks,
  scenarios), the two frozen evidence sets (their MANIFEST hashes, verified first) AND the downloaded revision of each model's weights. Change a prompt, a threshold, a rule or a frozen label and the hash changes.
* ``HeldOutRunner.run`` runs the held-out split ONCE per hash. A second call with the same hash is refused (re-running until the numbers look good is the named
  mistake); a different hash is a different configuration and may run. Dev runs are unlimited and are stored separately.
* ``deck_numbers`` returns the figures only from the stored held-out run for the CURRENT hash. With no such run, or with code that changed since, it refuses:
  a slide number cannot come from anywhere else.
The runner itself (what plays the scenarios and the labelled sets) is injected; this module owns the discipline, not the model calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable

from core.eval.matrix import config_hash
from core.eval.metrics import FROZEN_ROOT, verify_manifest


class HeldOutAlreadyRun(RuntimeError):
    pass


class NoFrozenRun(RuntimeError):
    pass


# The models a result depends on. Their NAMES are in the code (so config_hash covers them); their WEIGHTS can change under the same name, so the
# downloaded revision of each is part of the freeze hash too.
# The list is read from the code's own defaults, so it cannot drift from the models actually loaded.
from core.gate.embedder import DEFAULT_MODEL as _EMBEDDER          # noqa: E402
from core.gate.nli import DEFAULT_NLI_MODEL as _NLI                # noqa: E402
from core.gate.retrieval import DEFAULT_RERANKER as _RERANKER      # noqa: E402

MODELS = (_NLI, _EMBEDDER, _RERANKER)


def _hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def model_revisions(hf_cache: Path | str | None = None) -> dict[str, str]:
    """model name -> the downloaded revision (the Hugging Face cache's refs/main), or "absent" when it was never downloaded."""
    cache = Path(hf_cache) if hf_cache else _hf_cache()
    out = {}
    for name in MODELS:
        ref = cache / f"models--{name.replace('/', '--')}" / "refs" / "main"
        out[name] = ref.read_text(encoding="utf-8").strip() if ref.is_file() else "absent"
    return out


def freeze_hash(root: Path | str, frozen_root: Path | str | None = None, hf_cache: Path | str | None = None) -> str:
    root = Path(root)
    fr = Path(frozen_root) if frozen_root else FROZEN_ROOT
    parts = [config_hash(root)]
    for name in ("claims", "probes"):
        d = fr / name
        verify_manifest(d)                                   # a tampered frozen set stops everything before a hash exists
        parts.append(hashlib.sha256((d / "MANIFEST.sha256").read_bytes()).hexdigest()[:16])
    parts.append(json.dumps(model_revisions(hf_cache), sort_keys=True))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


class HeldOutRunner:
    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn, self.clock = conn, clock
        # The once-only rule in the database itself (also in schema.sql; created here for databases made before it existed): a check-then-insert race,
        # or code that skips the check, still cannot store a second held-out run for one hash.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS metrics_runs_one_heldout ON metrics_runs(config_hash) WHERE split = 'heldout'")

    def _runs(self, split: str, h: str):
        return self.conn.execute("SELECT * FROM metrics_runs WHERE split = ? AND config_hash = ? ORDER BY run_id", (split, h)).fetchall()

    def run(self, split: str, h: str, play: Callable[[], dict]) -> dict:
        """play() runs the scenarios and sets and returns the results dict. ``heldout`` is allowed once per hash."""
        if split not in ("dev", "heldout"):
            raise ValueError("split is 'dev' or 'heldout'")
        if split == "heldout" and self._runs("heldout", h):
            raise HeldOutAlreadyRun(f"the held-out set was already run for configuration {h}; change the configuration (a new hash) to run it again")
        started = self.clock.now_iso()
        results = play()
        n = self.conn.execute("SELECT COUNT(*) FROM metrics_runs").fetchone()[0] + 1
        try:
            self.conn.execute("INSERT INTO metrics_runs(run_id, config_hash, split, started_at, results_json) VALUES (?,?,?,?,?)",
                              (f"MR-{n:04d}", h, split, started, json.dumps(results, sort_keys=True)))
        except sqlite3.IntegrityError as e:
            raise HeldOutAlreadyRun(f"the held-out set was already run for configuration {h} (refused by the database)") from e
        return results

    def heldout_for(self, h: str) -> dict | None:
        rows = self._runs("heldout", h)
        return None if not rows else {"run_id": rows[0]["run_id"], "config_hash": h, "started_at": rows[0]["started_at"], "results": json.loads(rows[0]["results_json"])}


def deck_numbers(conn: sqlite3.Connection, current_hash: str) -> dict:
    """The only source for slide figures: the held-out run for the configuration that exists NOW."""
    run = HeldOutRunner(conn, None).heldout_for(current_hash)
    if run is None:
        other = conn.execute("SELECT config_hash FROM metrics_runs WHERE split = 'heldout' ORDER BY run_id DESC LIMIT 1").fetchone()
        why = (f"the only held-out run is for configuration {other['config_hash']}, not the current {current_hash}: the code or a frozen set changed after it"
               if other else "no held-out run exists yet")
        raise NoFrozenRun(why)
    return run


def matrix_store_from_run(run: dict) -> dict:
    """The held-out run in the shape core.eval.matrix.compute_matrix reads (#149): scenario id -> attempts. The run's results carry ``code_hash``
    (core.eval.matrix.config_hash at run time), so the matrix goes stale the moment the code moves on. It shows this run's hash and time."""
    scen = {sid: {"attempts": [bool(x) for x in attempts], "last_problem": ""} for sid, attempts in run["results"].get("scenarios", {}).items()}
    first = next(iter(scen.values()), {"attempts": []})
    return {"config_hash": run["results"].get("code_hash", run["config_hash"]), "runs": len(first["attempts"]),
            "last_run": {"run": run["run_id"], "recorded_at": run["started_at"], "config_hash": run["config_hash"]}, "scenarios": scen}
