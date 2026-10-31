"""One ranked list for human time, and how a lead is approached: value sets the level, not knowing raises it."""
import pytest

from jobs.approach import D2C_TOP, HUMAN, approach_for
from jobs.dedupe import DedupeJob
from jobs.health import Finding, HealthReport
from jobs.priority import P_LOSE, P_SAVE, at_risk_value, dry_run_health, merchant_items, ranked_work
from jobs.scoring import CAC_PAISE, score_ready_leads
from tests.checkpoint_script import SCRIPT
from tests.world import World


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    DedupeJob(w.conn, w.clock).run()
    score_ready_leads(w.conn, w.clock)
    return w


def report(*findings):
    return HealthReport(as_of="2026-10-31", findings=list(findings))


def finding(mid, diagnosis):
    return Finding(mid, diagnosis, "X", None, {"ratios": [0.4, 0.4, 0.4]})


def test_the_value_is_volume_times_chance_to_leave_times_chance_to_save_per_paise_of_cost():
    assert at_risk_value(100_000_000, 0.5, 0.7, 250_000) == 140
    assert at_risk_value(100_000_000, 0.5, 0.45, 40_000) == 562


def test_only_a_lasting_dip_with_no_service_explanation_is_retention_work(world):
    assert merchant_items(world.conn, report(finding("M-0001", "DEVICE_OFFLINE"), finding("M-0002", "SETTLEMENT_FAILURE"), finding("M-0003", "AREA_WIDE_DIP"), finding("M-0003", "AREA_OUTAGE"))) == []
    items = merchant_items(world.conn, report(finding("M-0003", "COMPETITOR_SWITCH")))
    assert [(i.kind, i.item_id, i.diagnosis) for i in items] == [("merchant_at_risk", "M-0003", "COMPETITOR_SWITCH")] and items[0].value > 0


def test_a_competitor_switch_outranks_an_unexplained_drop_for_the_same_merchant(world):
    a = merchant_items(world.conn, report(finding("M-0003", "COMPETITOR_SWITCH")))[0].value
    b = merchant_items(world.conn, report(finding("M-0003", "UNEXPLAINED_DROP")))[0].value
    assert a > b and P_LOSE["COMPETITOR_SWITCH"] > P_LOSE["UNEXPLAINED_DROP"]


def test_the_cheaper_better_save_channel_is_chosen_by_value(world):
    item = merchant_items(world.conn, report(finding("M-0003", "COMPETITOR_SWITCH")))[0]
    monthly = item.monthly_paise
    best = max(P_SAVE, key=lambda c: (at_risk_value(monthly, 0.5, P_SAVE[c], CAC_PAISE[c]), c))
    assert item.channel == best and item.value == at_risk_value(monthly, 0.5, P_SAVE[best], CAC_PAISE[best])


def test_merchants_and_leads_are_on_one_list_best_first_with_ties_by_id(world):
    items = ranked_work(world.conn, world.clock, report(finding("M-0003", "COMPETITOR_SWITCH")))
    assert {i.kind for i in items} == {"merchant_at_risk", "new_lead"}
    assert [(-i.value, i.kind, i.item_id) for i in items] == sorted((-i.value, i.kind, i.item_id) for i in items)
    assert all(i.kind != "new_lead" or world.conn.execute("SELECT matched_merchant_id FROM leads WHERE lead_id = ?", (i.item_id,)).fetchone()[0] is None for i in items)


def test_an_existing_merchant_is_never_a_new_lead_on_the_list(world):
    ids = {i.item_id for i in ranked_work(world.conn, world.clock, report()) if i.kind == "new_lead"}
    existing = {r[0] for r in world.conn.execute("SELECT lead_id FROM leads WHERE matched_merchant_id IS NOT NULL")}
    assert ids and not ids & existing


def test_looking_at_the_list_emits_nothing_and_stores_nothing(world):
    before = world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    dry_run_health(world.conn, world.clock)
    assert (world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], world.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == before
    assert world.conn.execute("SELECT name FROM sqlite_master WHERE name = 'merchant_health'").fetchone() is None or world.conn.execute("SELECT COUNT(*) FROM merchant_health").fetchone()[0] == 0


def test_the_dry_run_health_report_matches_what_the_real_job_would_find(world):
    from jobs.health import HealthJob
    dry = dry_run_health(world.conn, world.clock)
    real = HealthJob(world.conn, world.clock).run()
    assert [(f.merchant_id, f.diagnosis) for f in dry.findings] == [(f.merchant_id, f.diagnosis) for f in real.findings]


# ---------------------------------------------------------------------------------------------------- the approach
@pytest.mark.parametrize("value,level", [(0, "self_serve"), (1, "ai_voice"), (5, "ai_voice"), (6, "field_rep"), (400, "field_rep")])
def test_value_alone_sets_the_level_when_we_know_the_lead(value, level):
    a = approach_for(value, uncertain=False, category="kirana")
    assert a.level == level and a.base_level == level and a.bumped is False


@pytest.mark.parametrize("value,level", [(0, "ai_voice"), (3, "field_rep"), (50, "field_rep")])
def test_not_knowing_the_lead_moves_it_one_step_toward_a_person_and_never_further(value, level):
    a = approach_for(value, uncertain=True, category="kirana")
    assert a.level == level and (a.bumped is (value < 6))


def test_ignorance_never_lowers_the_level_and_the_reason_is_given():
    assert approach_for(50, uncertain=True, category="kirana").level == "field_rep"
    assert any("know little" in r for r in approach_for(0, uncertain=True, category="kirana").reasons)


def test_an_online_brand_has_no_shop_to_visit_so_its_top_level_is_a_human_call():
    assert approach_for(50, uncertain=False, category="d2c").level == D2C_TOP and approach_for(0, uncertain=False, category="d2c").level == "self_serve"
    assert approach_for(3, uncertain=True, category="d2c").level == D2C_TOP


@pytest.mark.parametrize("cat", ["bank", "nbfc", "institution"])
def test_a_bank_is_never_approached_by_a_message(cat):
    a = approach_for(500, uncertain=False, category=cat)
    assert a.level == HUMAN and not a.bumped
