"""#86 C10 Scam Shield reaches the supported bar: dev and held-out scenarios through the real World and scenario runner, decided by the
code handler (no model)."""
import os
from pathlib import Path

import pytest

from core.engine.scripted import scripted_decider
from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from packs.paytm_merchant import scam_shield as ss
from tests.scenario_runner import check, run_scenario
from tests.world import World

SC_DIR = Path(os.environ.get("MUNIM_SCENARIO_DIR") or Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios")
SCENARIOS = [load_scenario(p) for split in ("dev", "heldout") for p in sorted((SC_DIR / split).glob(f"{ss.PLAYBOOK_ID}__*.yaml"))]


def build(db_path, clock, mid, language):
    w = World(db_path, clock, None, mid=mid)
    h = ss.make_handler(w.conn, w.reader, w.clock, w.ledger)
    w.engine.decider = scripted_decider({(ss.PLAYBOOK_ID, "answer"): lambda ctx: h(ctx, language)})
    return w


def test_c10_meets_the_supported_bar_and_covers_every_family():
    cov = coverage(SCENARIOS)[ss.PLAYBOOK_ID]
    assert meets_supported_bar(cov), cov
    assert cov["families"] == ["adversarial", "happy", "multi_case", "noise", "should_escalate"], cov


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    w = build(db_path, clock, sc.persona.merchant_id, sc.persona.language)
    assert check(sc, run_scenario(w, sc)) == []
