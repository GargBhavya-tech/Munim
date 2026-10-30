"""#108 rep matching: hard rules hold in every assignment; the score is language + shrunk win rate + continuity + seniority-to-value - workload."""
import inspect
from pathlib import Path

import pytest

import field.matching as matching
from field.matching import HIGH_VALUE_PAISE, LeadNeed, Weights, hard_rule_failures, match_from_db, match_leads, score_candidate
from field.privacy import FieldPrivacy
from field.profiles import RepProfile

NEAR = lambda a, b: 10.0          # noqa: E731  every lead is ten minutes from every base
BLR = (12.97, 77.59)


def rep(rid, langs, *, months=24, wr=None, training=("soundbox", "card_machine", "loans"), stock=None, shift=(600, 1140), base=BLR):
    return RepProfile(rid, rid, base, shift, langs, months, wr or {"kirana": 0.25}, tuple(training), stock if stock is not None else {"soundbox": 5, "card_machine": 5})


def lead(lid, *, lang="kn-IN", only=False, product="soundbox", value=1_000_000, seg="kirana", window=(600, 1140), owner=None, point=BLR):
    return LeadNeed(lid, seg, product, lang, value, point, only, window, owner)


KN = {"kn-IN": "native", "en-IN": "fluent"}
HI = {"hi-IN": "native", "en-IN": "fluent"}


# ---------------------------------------------------------------- Watch out: language treated as a preference
def test_kannada_only_lead_never_goes_to_a_non_kannada_rep_even_when_that_rep_scores_far_higher():
    reps = [rep("R-HI", HI, wr={"kirana": 0.9}, months=40), rep("R-KN", KN, wr={"kirana": 0.05}, months=6)]
    res = match_leads(reps, [lead("L-1", only=True)], NEAR)[0]
    assert res.rep_id == "R-KN" and res.rejected == {"R-HI": "LANGUAGE"}


def test_kannada_only_lead_with_no_kannada_rep_is_left_unassigned_not_given_to_the_best_available():
    res = match_leads([rep("R-HI", HI, wr={"kirana": 0.9})], [lead("L-1", only=True)], NEAR)[0]
    assert res.rep_id is None and res.reason == "NO_ELIGIBLE_REP"


def test_basic_level_does_not_count_as_speaking_the_language():
    basic = rep("R-B", {"kn-IN": "basic", "hi-IN": "native"})
    assert match_leads([basic], [lead("L-1", only=True)], NEAR)[0].rep_id is None
    assert match_leads([basic], [lead("L-1", only=False)], NEAR)[0].rep_id == "R-B"        # not language-only: a preference, so still allowed


def test_language_is_only_a_score_when_the_merchant_is_not_language_only():
    reps = [rep("R-HI", HI), rep("R-KN", KN)]
    res = match_leads(reps, [lead("L-1", only=False)], NEAR)[0]
    assert res.rep_id == "R-KN" and res.parts["language"] == 3.0            # native = 3 * 1.0; the Hindi rep scores 0 on language


# ---------------------------------------------------------------- the other hard rules
def test_training_stock_shift_and_territory_each_exclude():
    a = rep("A", KN, training=("soundbox",))
    assert hard_rule_failures(a, lead("L", product="loans"), NEAR, 60, 0, 8) == ["TRAINING"]
    b = rep("B", KN, stock={"soundbox": 0})
    assert hard_rule_failures(b, lead("L"), NEAR, 60, 0, 8) == ["STOCK"]
    assert hard_rule_failures(rep("C", KN, shift=(600, 720)), lead("L", window=(900, 1000)), NEAR, 60, 0, 8) == ["SHIFT"]
    assert hard_rule_failures(rep("D", KN), lead("L"), lambda x, y: 75.0, 60, 0, 8) == ["TERRITORY"]
    assert hard_rule_failures(rep("E", KN), lead("L"), NEAR, 60, 8, 8) == ["WORKLOAD_CAP"]
    assert hard_rule_failures(rep("F", KN), lead("L"), NEAR, 60, 0, 8) == []


def test_loan_lead_skips_the_rep_who_is_not_loan_trained_even_if_she_is_the_only_kannada_speaker_nearby():
    reps = [rep("NEW", KN, months=8, training=("soundbox",)), rep("SR", HI, months=30)]
    res = match_leads(reps, [lead("L-1", product="loans", lang="hi-IN", only=True)], NEAR)[0]
    assert res.rep_id == "SR"


def test_stock_is_used_up_so_the_second_soundbox_lead_cannot_go_to_a_rep_with_one_unit():
    reps = [rep("ONE", KN, stock={"soundbox": 1}, months=30)]
    res = match_leads(reps, [lead("L-1", value=2_000_000), lead("L-2", value=1_000_000)], NEAR)
    assert [(r.lead_id, r.rep_id) for r in res] == [("L-1", "ONE"), ("L-2", None)] and res[1].rejected == {"ONE": "STOCK"}


# ---------------------------------------------------------------- score
def test_score_is_the_sum_of_its_documented_parts_with_exact_values():
    r = rep("A", KN, months=12, wr={"kirana": 0.40})
    s, parts = score_candidate(r, lead("L", owner="A", value=HIGH_VALUE_PAISE), prior=0.20, visits=10, assigned=2, w=Weights())
    # language native 1.0*3 ; win = (0.40*10 + 0.20*10)/20 = 0.30 *10 ; continuity 2 ; seniority 12/24=0.5, high value wants 1.0 -> fit 0.5 *1.5 ; workload -2
    assert parts == {"language": 3.0, "win_rate": 3.0, "continuity": 2.0, "seniority": 0.75, "workload": -2.0} and s == 6.75


def test_thin_evidence_win_rate_is_pulled_toward_the_team_prior():
    hot = rep("HOT", KN, wr={"kirana": 0.9})            # 0.9 on 2 visits
    steady = rep("STEADY", KN, wr={"kirana": 0.65})     # 0.65 on 200 visits
    low = rep("LOW", KN, wr={"kirana": 0.1})            # team prior = mean(0.9, 0.65, 0.1) = 0.55
    thin = {("HOT", "kirana"): 2, ("STEADY", "kirana"): 200, ("LOW", "kirana"): 200}
    assert match_leads([hot, steady, low], [lead("L-1")], NEAR, visit_counts=thin)[0].rep_id == "STEADY"      # HOT shrinks to 0.608
    solid = {**thin, ("HOT", "kirana"): 200}
    assert match_leads([hot, steady, low], [lead("L-1")], NEAR, visit_counts=solid)[0].rep_id == "HOT"


def test_continuity_prefers_the_rep_who_already_owns_the_merchant():
    reps = [rep("A", KN, wr={"kirana": 0.30}), rep("B", KN, wr={"kirana": 0.31})]
    assert match_leads(reps, [lead("L-1")], NEAR)[0].rep_id == "B"
    assert match_leads(reps, [lead("L-1", owner="A")], NEAR)[0].rep_id == "A"


def test_high_value_goes_to_the_senior_rep_and_low_value_leaves_seniors_free():
    reps = [rep("JUNIOR", KN, months=4), rep("SENIOR", KN, months=40)]
    assert match_leads(reps, [lead("L-1", value=HIGH_VALUE_PAISE)], NEAR)[0].rep_id == "SENIOR"
    assert match_leads(reps, [lead("L-1", value=100_000)], NEAR)[0].rep_id == "JUNIOR"


def test_leads_are_spread_fairly_not_piled_on_the_best_rep():
    reps = [rep("A", KN, wr={"kirana": 0.30}), rep("B", KN, wr={"kirana": 0.29})]
    res = match_leads(reps, [lead(f"L-{i}", value=1_000_000 - i) for i in range(6)], NEAR)
    counts = {r: sum(1 for x in res if x.rep_id == r) for r in ("A", "B")}
    assert counts == {"A": 3, "B": 3}


def test_the_workload_cap_stops_a_rep_taking_more_than_max_leads():
    res = match_leads([rep("A", KN)], [lead(f"L-{i}") for i in range(5)], NEAR, max_leads=3)
    assert sum(1 for r in res if r.rep_id == "A") == 3 and sum(1 for r in res if r.reason == "NO_ELIGIBLE_REP") == 2


def test_matching_is_deterministic():
    reps = [rep("A", KN), rep("B", KN)]
    leads = [lead(f"L-{i}") for i in range(5)]
    assert [r.__dict__ for r in match_leads(reps, leads, NEAR)] == [r.__dict__ for r in match_leads(list(reversed(reps)), list(reversed(leads)), NEAR)]


# ---------------------------------------------------------------- privacy rules
def test_a_sneaked_in_protected_column_stops_matching(conn):
    conn.execute("ALTER TABLE reps ADD COLUMN community TEXT")
    with pytest.raises(AssertionError):
        match_from_db(conn, [lead("L-1")], NEAR)


def test_seeded_reps_match_and_coaching_notes_change_nothing(conn, clock):
    leads = [lead("L-1", lang="kn-IN", only=True, product="soundbox", seg="restaurant"), lead("L-2", lang="hi-IN", seg="kirana")]
    conn.execute("UPDATE reps SET stock_json = '{\"soundbox\": 5, \"card_machine\": 5}'")
    before = [(r.lead_id, r.rep_id, r.score) for r in match_from_db(conn, leads, NEAR)]
    assert before[0][1] in ("R-01", "R-02")                                   # the Kannada-only lead goes to a native Kannada speaker
    p = FieldPrivacy(conn, clock)
    conn.execute("DELETE FROM consent WHERE subject_id = 'R-01' AND channel = 'call_analysis'")
    cols = [c[1] for c in conn.execute("PRAGMA table_info(consent)")]
    vals = {"subject_id": "R-01", "channel": "call_analysis", "status": "GRANTED"}
    for c in cols:
        if c not in vals and c in ("updated_at", "granted_at", "ts", "created_at"):
            vals[c] = clock.now_iso()
    conn.execute(f"INSERT INTO consent({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
    p.analyse_visit("R-01", "V-1", {"strengths": ["listens"], "suggestions": ["be brief"] * 9})
    p.analyse_visit("R-01", "V-2", {"strengths": [], "suggestions": ["x"] * 30})
    assert [(r.lead_id, r.rep_id, r.score) for r in match_from_db(conn, leads, NEAR)] == before


def test_the_matching_module_never_reads_coaching_or_protected_data():
    src = Path(inspect.getsourcefile(matching)).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')))
    assert "coaching_notes" not in code and "field.privacy" not in code and "analyse_visit" not in code
    assert not any(p in str(inspect.signature(score_candidate)).lower() for p in ("coach", "religion", "caste", "community"))
