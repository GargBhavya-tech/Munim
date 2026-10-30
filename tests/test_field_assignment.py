"""#109 technician assignment: hard rules (skill, parts, shift, language) hold in every assignment (gate G5); durability outweighs speed."""
import random

import pytest

from field.assignment import (AssignWeights, IssueHistory, Job, assign_from_db, assign_jobs, hard_rule_failures, load_history, record_history, score_tech,
                              team_rates)
from field.profiles import TechProfile, load_techs


def tech(tid, *, skills=("soundbox_repair",), parts=None, langs=None, shift=(540, 1080), cap=8):
    return TechProfile(tid, tid, (12.9, 77.6), shift, langs or {"kn-IN": "native", "hi-IN": "fluent"}, frozenset(skills), (), parts if parts is not None else {"soundbox_unit": 3}, cap)


def job(jid, **kw):
    base = dict(merchant_id="M-1", issue_type="offline", skill="soundbox_repair", needed_parts={"soundbox_unit": 1}, language="kn-IN")
    return Job(jid, **{**base, **kw})


# ---------------------------------------------------------------- hard rules
def test_each_hard_rule_excludes_and_names_itself():
    assert hard_rule_failures(tech("A", skills=("network",)), job("J"), 0) == ["SKILL"]
    assert hard_rule_failures(tech("A", parts={"soundbox_unit": 0}), job("J"), 0) == ["PARTS"]
    assert hard_rule_failures(tech("A", langs={"hi-IN": "native"}), job("J", language_only=True), 0) == ["LANGUAGE"]
    assert hard_rule_failures(tech("A", langs={"hi-IN": "native"}), job("J", language_only=False), 0) == []
    assert hard_rule_failures(tech("A", shift=(540, 600)), job("J", window=(900, 1000)), 0) == ["SHIFT"]
    assert hard_rule_failures(tech("A", cap=2), job("J"), 2) == ["DAILY_CAP"]


def test_a_job_nobody_qualifies_for_is_unassigned_with_reasons_not_given_to_the_nearest():
    res = assign_jobs([tech("A", skills=("network",)), tech("B", parts={})], [job("J")], {})[0]
    assert res.tech_id is None and res.reason == "NO_ELIGIBLE_TECHNICIAN" and res.rejected == {"A": "SKILL", "B": "PARTS"}


def test_parts_are_used_up_across_a_batch():
    res = assign_jobs([tech("A", parts={"soundbox_unit": 1})], [job("J1", priority=2), job("J2", priority=1)], {})
    assert [(r.job_id, r.tech_id) for r in res] == [("J1", "A"), ("J2", None)] and res[1].rejected == {"A": "PARTS"}


def test_g5_no_assignment_in_a_randomised_batch_breaks_a_skill_language_or_parts_rule():
    rnd = random.Random(7)
    skills = ["network", "soundbox_repair", "installation", "card_machine"]
    langs = [{"kn-IN": "native"}, {"hi-IN": "native"}, {"kn-IN": "basic", "hi-IN": "native"}, {"kn-IN": "fluent", "en-IN": "fluent"}]
    techs = [tech(f"T{i}", skills=rnd.sample(skills, 2), parts={"soundbox_unit": rnd.randint(0, 3), "adapter": rnd.randint(0, 3)}, langs=rnd.choice(langs),
                  shift=(rnd.choice([480, 540, 600]), rnd.choice([960, 1080])), cap=rnd.randint(1, 4)) for i in range(12)]
    jobs = [Job(f"J{i}", f"M-{i}", "offline", rnd.choice(skills), rnd.choice([{}, {"soundbox_unit": 1}, {"adapter": 2}]), rnd.choice(["kn-IN", "hi-IN"]),
               rnd.random() < 0.5, (rnd.choice([480, 700, 900]), 1000)) for i in range(80)]
    res = assign_jobs(techs, jobs, {})
    by_job, by_tech = {j.job_id: j for j in jobs}, {t.tech_id: t for t in techs}
    used, load = {}, {}
    assigned = 0
    for a in res:
        if a.tech_id is None:
            continue
        assigned += 1
        j, t = by_job[a.job_id], by_tech[a.tech_id]
        assert j.skill in t.skills
        assert not j.language_only or t.speaks(j.language)
        assert max(t.shift[0], j.window[0]) < min(t.shift[1], j.window[1])
        load[t.tech_id] = load.get(t.tech_id, 0) + 1
        for k, n in j.needed_parts.items():
            used[(t.tech_id, k)] = used.get((t.tech_id, k), 0) + n
    assert assigned > 10 and all(n <= by_tech[t].max_jobs_per_day for t, n in load.items())
    assert all(n <= by_tech[t].parts.get(k, 0) for (t, k), n in used.items())


# ---------------------------------------------------------------- Watch out: speed dominating durability
def test_a_fast_technician_whose_fixes_do_not_hold_loses_to_a_slower_one_whose_fixes_hold():
    hist = {("FAST", "offline"): IssueHistory(jobs=100, fixed=90, durable=45, avg_minutes=20),
            ("SLOW", "offline"): IssueHistory(jobs=100, fixed=90, durable=85, avg_minutes=110)}
    res = assign_jobs([tech("FAST"), tech("SLOW")], [job("J")], hist)[0]
    assert res.tech_id == "SLOW"
    assert res.parts["durability"] > 0 and AssignWeights().speed < AssignWeights().durability / 4


def test_speed_still_breaks_a_tie_between_equally_durable_technicians():
    hist = {("FAST", "offline"): IssueHistory(100, 90, 85, 30), ("SLOW", "offline"): IssueHistory(100, 90, 85, 110)}
    assert assign_jobs([tech("SLOW"), tech("FAST")], [job("J")], hist)[0].tech_id == "FAST"


# ---------------------------------------------------------------- score parts, case mix, continuity, workload
def test_score_parts_have_exact_values():
    hist = {("A", "offline"): IssueHistory(10, 8, 6, 60)}                      # team = only A: base fix 0.8, base durability 0.75
    s, p = score_tech(tech("A"), job("J", owner_tech="A"), hist, assigned=1, w=AssignWeights())
    # fix: raw 0.8, n 10, prior 0.8 -> 0.8*10 ; durability raw 6/8 prior 0.75 -> 0.75*8 ; speed 60/60-0.5=0.5 ; continuity 2 ; workload -1
    assert p == {"fix_rate": 8.0, "continuity": 2.0, "durability": 6.0, "speed": 0.5, "workload": -1.0} and s == 15.5


def test_case_mix_fix_rate_is_judged_against_the_team_rate_for_that_issue_type():
    hist = {("X", "easy"): IssueHistory(40, 38, 36, 60), ("Y", "easy"): IssueHistory(40, 38, 36, 60),
            ("X", "hard"): IssueHistory(40, 18, 16, 60), ("Y", "hard"): IssueHistory(40, 18, 16, 60)}
    easy, hard = (job("J1", issue_type="easy"), job("J2", issue_type="hard"))
    # 95% on easy jobs and 45% on hard jobs are each exactly the team norm for that type: neither is read as better or worse than the other
    assert score_tech(tech("X"), easy, hist, 0, AssignWeights())[1]["fix_rate"] == pytest.approx(9.5)
    assert score_tech(tech("X"), hard, hist, 0, AssignWeights())[1]["fix_rate"] == pytest.approx(4.5)
    # a technician's easy record gives no credit on a hard job they have never done: they get the team norm for hard jobs
    hist[("Z", "easy")] = IssueHistory(40, 40, 40, 60)
    assert score_tech(tech("Z"), hard, hist, 0, AssignWeights())[1]["fix_rate"] == pytest.approx(4.5, abs=0.01)


def test_two_lucky_wins_do_not_outrank_a_long_good_record():
    hist = {("PRO", "offline"): IssueHistory(100, 90, 80, 60), ("LOW", "offline"): IssueHistory(100, 40, 30, 60), ("NEW", "offline"): IssueHistory(2, 2, 2, 60)}
    assert assign_jobs([tech("NEW"), tech("PRO")], [job("J")], hist)[0].tech_id == "PRO"
    fix = score_tech(tech("NEW"), job("J"), hist, 0, AssignWeights())[1]["fix_rate"]
    assert fix == pytest.approx(7.1122, abs=0.001)             # (2 + 10 * 0.6535) / 12 * 10, nowhere near the raw 10.0


def test_continuity_and_workload():
    hist = {("A", "offline"): IssueHistory(50, 40, 36, 60), ("B", "offline"): IssueHistory(50, 40, 36, 60)}
    assert assign_jobs([tech("A"), tech("B")], [job("J", owner_tech="B")], hist)[0].tech_id == "B"
    res = assign_jobs([tech("A"), tech("B")], [job(f"J{i}", needed_parts={}) for i in range(6)], hist)
    assert sorted(r.tech_id for r in res) == ["A", "A", "A", "B", "B", "B"]


def test_urgent_jobs_are_assigned_first_so_scarce_parts_go_to_them():
    res = assign_jobs([tech("A", parts={"soundbox_unit": 1})], [job("LOW", priority=0), job("SLA", priority=9)], {})
    assert {r.job_id: r.tech_id for r in res} == {"SLA": "A", "LOW": None}


# ---------------------------------------------------------------- DB entry
def test_db_history_round_trips_and_a_protected_column_stops_assignment(conn):
    record_history(conn, "T-07", "offline", jobs=20, fixed=18, durable=16, avg_minutes=45)
    assert load_history(conn) == {("T-07", "offline"): IssueHistory(20, 18, 16, 45)}
    with pytest.raises(ValueError):
        record_history(conn, "T-07", "offline", jobs=5, fixed=6, durable=1, avg_minutes=1)
    conn.execute("UPDATE technicians SET parts_json = '{\"soundbox_unit\": 2}'")
    res = assign_from_db(conn, [job("J", language_only=True, language="kn-IN")])[0]
    assert res.tech_id is not None and next(t for t in load_techs(conn) if t.tech_id == res.tech_id).speaks("kn-IN")
    conn.execute("ALTER TABLE technicians ADD COLUMN caste TEXT")
    with pytest.raises(AssertionError):
        assign_from_db(conn, [job("J")])
