"""#127 rival-network analysis. E13: with the legal flag off, individual targeting is blocked in every path; aggregates never carry a payee."""
import copy
import dataclasses

import pytest

from core.audit.ledger import Ledger
from core.engine.policy import load_limits
from jobs.discovery import DiscoveryJob
from jobs.rival import MIN_PAYEES, IndividualTargetingBlocked, Observation, RivalAnalysis, synthetic_observations
from jobs.synthetic_sources import make_fetcher
from packs.paytm_merchant.policy.rules import LIMITS_PATH, build_engine


@pytest.fixture
def ledger(conn, clock):
    return Ledger(conn, clock)


@pytest.fixture
def ra(conn, clock, ledger):
    return RivalAnalysis(conn, clock, ledger)


@pytest.fixture
def ra_on(conn, clock, ledger):
    lim = copy.deepcopy(load_limits(LIMITS_PATH))
    lim["flags"]["legal_approval"] = True
    return RivalAnalysis(conn, clock, ledger, engine=build_engine(conn, clock, limits=lim))


OBS = synthetic_observations()
REFS = {o.payee_ref for o in OBS}


def dump(conn):
    return " ".join(str(v) for t in ("rival_aggregates", "ledger", "events", "leads") for row in conn.execute(f"SELECT * FROM {t}") for v in tuple(row))


def test_aggregates_have_exact_integer_values_per_neighbourhood(ra):
    rows = {r.pincode: r for r in ra.aggregate(OBS)}
    rival = [o for o in OBS if o.network == "rivalpay" and o.pincode == "560034"]
    own = sum(o.volume_paise for o in OBS if o.network == "paytm" and o.pincode == "560034")
    r = rows["560034"]
    assert r.rival_payees == 14 and not r.suppressed
    assert r.rival_volume_paise == sum(o.volume_paise for o in rival) and r.rival_txns == sum(o.txn_count for o in rival) and r.own_volume_paise == own
    assert r.rival_share_bp == r.rival_volume_paise * 10_000 // (r.rival_volume_paise + own) and isinstance(r.rival_share_bp, int)


def test_a_thin_cell_is_suppressed_so_it_cannot_point_at_a_shop(ra, conn):
    rows = {r.pincode: r for r in ra.aggregate(OBS)}
    thin = rows["560068"]
    assert thin.rival_payees == 3 < MIN_PAYEES and thin.suppressed
    assert thin.rival_volume_paise is None and thin.rival_share_bp is None and thin.own_volume_paise is None
    stored = conn.execute("SELECT * FROM rival_aggregates WHERE pincode='560068'").fetchone()
    assert stored["suppressed"] == 1 and stored["rival_volume_paise"] is None
    assert "560068" not in ra.priority_pincodes(list(rows.values()), 10)
    four = [Observation("560001", "rivalpay", f"Z-{i}", 1, 1000) for i in range(MIN_PAYEES - 1)]
    assert ra.aggregate(four)[0].suppressed and not ra.aggregate([*four, Observation("560001", "rivalpay", "Z-9", 1, 1000)])[0].suppressed


def test_priority_pincodes_rank_by_rival_volume_and_exclude_suppressed(ra):
    rows = ra.aggregate(OBS)
    vol = {r.pincode: r.rival_volume_paise for r in rows if not r.suppressed}
    assert ra.priority_pincodes(rows, 5) == sorted(vol, key=lambda p: (-vol[p], p)) and len(vol) == 2


def test_no_neighbourhood_path_ever_carries_a_payee_reference(ra, conn, ledger):
    rows = ra.aggregate(OBS)
    assert "payee" not in {f.name for f in dataclasses.fields(rows[0])} - {"rival_payees"}
    ra.priority_pincodes(rows)
    job = DiscoveryJob(conn, ra.clock, ledger)
    res = ra.discover_candidates(job, {"gst_api": make_fetcher("gst_api")}, rows, n=2)
    assert res is not None and len(res.stored) > 0
    blob = repr(rows) + repr(res) + dump(conn)
    assert not any(ref in blob for ref in REFS)


def test_discovery_gets_only_the_top_neighbourhood_pincodes(ra, conn, ledger):
    seen = []
    job = DiscoveryJob(conn, ra.clock, ledger)
    ra.discover_candidates(job, {"gst_api": lambda pins: seen.append(list(pins)) or []}, ra.aggregate(OBS), n=1)
    assert seen == [["560034"]]


# ---- E13: flag off blocks individual targeting in every path -----------------------------------------------------
def test_e13_flag_off_blocks_every_individual_path_and_ledgers_it(ra, ledger, conn):
    before = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    with pytest.raises(IndividualTargetingBlocked) as e:
        ra.individual_targets(OBS)
    assert e.value.rule_ids == ("RIVAL-TARGET-001",)
    with pytest.raises(IndividualTargetingBlocked):
        ra.score_payee(OBS[0])                                             # the scorer is enforced too, not just the list builder
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == before + 2


def test_every_public_method_that_touches_a_payee_is_flag_gated(ra):
    """If someone adds a payee-level method, it must be listed here with its guard; this fails until they do."""
    public = {n for n in dir(RivalAnalysis) if not n.startswith("_")}
    assert public == {"aggregate", "priority_pincodes", "discover_candidates", "individual_targets", "score_payee"}


def test_flag_on_through_the_policy_limits_allows_individual_targeting(ra_on):
    out = ra_on.individual_targets(OBS, limit=3)
    assert len(out) == 3 and out == sorted(out, key=lambda t: (-t.volume_paise, t.payee_ref))
    assert all(t.network == "rivalpay" for t in out) and out[0].score == out[0].volume_paise // 10_000
    assert ra_on.score_payee(OBS[0]) == OBS[0].volume_paise // 10_000


def test_the_flag_is_read_from_policy_not_from_the_module(conn, clock, ledger):
    """Watch out: the flag readable but not enforced. Turning it on in policy limits alone changes the outcome; nothing in jobs/rival.py reads it."""
    import pathlib
    assert "legal_approval" not in pathlib.Path("jobs/rival.py").read_text(encoding="utf-8")
    off = RivalAnalysis(conn, clock, ledger)
    with pytest.raises(IndividualTargetingBlocked):
        off.individual_targets(OBS)
    lim = copy.deepcopy(load_limits(LIMITS_PATH))
    lim["flags"]["legal_approval"] = True
    assert RivalAnalysis(conn, clock, ledger, engine=build_engine(conn, clock, limits=lim)).individual_targets(OBS, limit=1)


def test_deterministic_synthetic_input():
    assert synthetic_observations(7) == synthetic_observations(7) != synthetic_observations(8)
