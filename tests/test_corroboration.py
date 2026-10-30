"""Corroboration levels (#46): exhaustive, total, unique, categorical."""
from __future__ import annotations

import inspect
import itertools

import pytest

import core.gate.corroboration as corr
from core.gate.corroboration import Evidence, Level, LevelResult, level

KINDS = ("tool", "kb", "policy")
# every distinct single-source state: (entailed, contradicted, forbids_rule)
STATES = [(e, c, f) for e in (False, True) for c in (False, True) for f in (None, "RULE-X")]


def ev(kind, state):
    e, c, f = state
    return Evidence(kind, entailed=e, contradicted=c, forbids_rule=f)


def oracle(items):
    """Independent restatement of the Bible table, written as plain predicates."""
    bad = [i for i in items if i.contradicted or i.forbids_rule]
    if bad:
        return Level.CONTRADICTED
    tool = [i for i in items if i.kind == "tool" and i.entailed]
    doc = [i for i in items if i.kind != "tool" and i.entailed]
    if tool and doc:
        return Level.CORROBORATED
    if tool or doc:
        return Level.SUPPORTED
    return Level.UNSUPPORTED


def all_evidence_lists(max_len):
    singles = [ev(k, s) for k in KINDS for s in STATES]   # 3 * 8 = 24 distinct items
    for n in range(0, max_len + 1):
        yield from itertools.product(singles, repeat=n)


def test_exhaustive_every_combination_maps_to_exactly_one_level():
    seen = set()
    count = 0
    for items in all_evidence_lists(2):   # 1 + 24 + 576 lists
        r = level(items)
        count += 1
        assert isinstance(r, LevelResult) and isinstance(r.level, Level)      # total: always a level, never None / error
        assert r.level == oracle(items), items                                # unique and correct
        assert sum(r.level == lv for lv in Level) == 1
        seen.add(r.level)
    assert count == 1 + 24 + 24 * 24
    assert seen == set(Level)   # all four levels are reachable


def test_exhaustive_triples_of_unflagged_and_flagged_kinds():
    base = [Evidence(k, entailed=e) for k in KINDS for e in (False, True)] + [Evidence(k, contradicted=True) for k in KINDS]
    for items in itertools.product(base, repeat=3):
        assert level(items).level == oracle(items)


def test_order_of_evidence_does_not_matter():
    pool = [Evidence("tool", entailed=True), Evidence("kb", entailed=True), Evidence("policy", forbids_rule="R-1"), Evidence("kb")]
    for n in range(0, 5):
        for combo in itertools.combinations(pool, n):
            results = {level(p).level for p in itertools.permutations(combo)}
            assert len(results) == 1


@pytest.mark.parametrize("items,expected", [
    ([], Level.UNSUPPORTED),
    ([Evidence("tool")], Level.UNSUPPORTED),
    ([Evidence("kb"), Evidence("policy")], Level.UNSUPPORTED),
    ([Evidence("tool", entailed=True)], Level.SUPPORTED),
    ([Evidence("kb", entailed=True)], Level.SUPPORTED),
    ([Evidence("policy", entailed=True)], Level.SUPPORTED),
    ([Evidence("kb", entailed=True), Evidence("policy", entailed=True)], Level.SUPPORTED),   # two documents, no system of record
    ([Evidence("tool", entailed=True), Evidence("tool", entailed=True)], Level.SUPPORTED),   # two tools, no document
    ([Evidence("tool", entailed=True), Evidence("kb", entailed=True)], Level.CORROBORATED),
    ([Evidence("tool", entailed=True), Evidence("policy", entailed=True)], Level.CORROBORATED),
    ([Evidence("tool", entailed=True), Evidence("kb")], Level.SUPPORTED),                    # kb silent: not corroborated
])
def test_named_cases(items, expected):
    assert level(items).level == expected


def test_contradiction_beats_support_and_corroboration():
    corroborated = [Evidence("tool", entailed=True), Evidence("kb", entailed=True)]
    assert level(corroborated).level == Level.CORROBORATED
    for extra in (Evidence("kb", contradicted=True), Evidence("tool", contradicted=True),
                  Evidence("policy", contradicted=True), Evidence("policy", forbids_rule="LOAN-RATE-001")):
        assert level(corroborated + [extra]).level == Level.CONTRADICTED
        assert level([extra] + corroborated).level == Level.CONTRADICTED
    # one item that is both entailed and contradicted is still CONTRADICTED
    assert level([Evidence("tool", entailed=True, contradicted=True)]).level == Level.CONTRADICTED
    assert level([Evidence("tool", entailed=True), Evidence("kb", contradicted=True)]).level == Level.CONTRADICTED


def test_forbidding_rule_id_is_carried():
    r = level([Evidence("tool", entailed=True), Evidence("policy", forbids_rule="LOAN-RATE-001")])
    assert (r.level, r.rule_id) == (Level.CONTRADICTED, "LOAN-RATE-001")
    r = level([Evidence("policy", forbids_rule="SECRET-001"), Evidence("policy", forbids_rule="LOAN-RATE-001")])
    assert r.rule_id == "SECRET-001"   # first forbidding rule in evidence order
    assert level([Evidence("kb", contradicted=True)]).rule_id is None    # contradicted by a source, no rule
    for items in ([], [Evidence("tool", entailed=True)], [Evidence("tool", entailed=True), Evidence("kb", entailed=True)]):
        assert level(items).rule_id is None


def test_chip_colours_and_sendability():
    c = level([Evidence("tool", entailed=True), Evidence("kb", entailed=True)])
    assert (c.chip, c.color, c.double_tick, c.sendable) == ("green double-tick", "green", True, True)
    s = level([Evidence("kb", entailed=True)])
    assert (s.chip, s.color, s.double_tick, s.sendable) == ("green", "green", False, True)
    u = level([])
    assert (u.chip, u.color, u.double_tick, u.sendable) == ("red", "red", False, False)
    x = level([Evidence("kb", contradicted=True)])
    assert (x.chip, x.color, x.double_tick, x.sendable) == ("red", "red", False, False)
    assert {lv.value for lv in Level} == {"CORROBORATED", "SUPPORTED", "UNSUPPORTED", "CONTRADICTED"}


def test_invalid_kind_rejected():
    with pytest.raises(ValueError):
        Evidence("blog", entailed=True)


def test_no_weighted_score_anywhere():
    """Watch out: a weighted score creeping in. Evidence and results are categorical; no numeric field exists."""
    for cls in (Evidence, LevelResult):
        for name, f in cls.__dataclass_fields__.items():
            assert f.type in ("str", "bool", "str | None", "Level"), (cls.__name__, name, f.type)
    r = level([Evidence("tool", entailed=True)])
    assert not any(isinstance(v, float) for v in vars(r).values())
    with pytest.raises(TypeError):
        Evidence("tool", entailed=True, score=0.9)       # type: ignore[call-arg]
    src = inspect.getsource(corr)
    for word in ("weight", "confidence", "probab", "threshold"):
        assert word not in src.lower().replace("weighted score", "").replace("no weighted", ""), word
