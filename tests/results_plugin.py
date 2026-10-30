"""Records the outcome of every scenario test so the coverage matrix (#69) is computed from test results, not typed in.

Enable with ``pytest --record-scenarios`` (optionally ``--scenario-results PATH``). Each run appends one attempt per scenario to
eval/out/scenario_results.json, keyed by the hash of the code and pack it ran against; held-out pass^4 therefore needs four runs.
Loaded from tests/conftest.py. Reads the wall clock for the 'recorded at' stamp, which is why it lives under tests/.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.eval.matrix import SCENARIO_TEST, config_hash, load_store, record_outcomes

ROOT = Path(__file__).resolve().parent.parent
_ID = re.compile(rf"::{SCENARIO_TEST}\[(.+)\]$")


_OUT: dict = {}


def pytest_addoption(parser):
    g = parser.getgroup("munim")
    g.addoption("--record-scenarios", action="store_true", help="append scenario test outcomes to the coverage results file")
    g.addoption("--scenario-results", default=str(ROOT / "eval" / "out" / "scenario_results.json"))
    g.addoption("--scenario-split", default=None, choices=["dev", "heldout"],
                help="run ONLY the scenario tests of this split (used by `munim eval`; the held-out split is Gate 4's, run once)")


def pytest_collection_modifyitems(config, items):
    split = config.getoption("--scenario-split", default=None)
    if not split:
        return
    from core.eval.scenario import load_scenarios
    ids = set()
    for scen_dir in sorted((ROOT / "packs").glob("*/scenarios")):
        ids |= {s.id for s in load_scenarios(scen_dir) if s.split == split}
    keep, drop = [], []
    for it in items:
        m = _ID.search(it.nodeid)
        (keep if m and m.group(1) in ids else drop).append(it)
    items[:] = keep
    config.hook.pytest_deselected(items=drop)


def pytest_runtest_logreport(report):
    m = _ID.search(report.nodeid)
    if not m or report.skipped:
        return
    # 'call' carries pass/fail; a setup/teardown error also counts as a failed attempt
    if report.when == "call" or (report.failed and report.when in ("setup", "teardown")):
        _OUT[m.group(1)] = (report.passed, report.longreprtext.splitlines()[-1] if report.failed else "")


def pytest_sessionfinish(session, exitstatus):
    cfg = session.config
    if not cfg.getoption("--record-scenarios", default=False) or not _OUT:
        return
    path = Path(cfg.getoption("--scenario-results"))
    store = record_outcomes(load_store(path), config_hash(ROOT), _OUT, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=1, sort_keys=True), encoding="utf-8")
