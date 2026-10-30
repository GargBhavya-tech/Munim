"""#156 deck: every number traces to the held-out run, a VERIFIED Paytm figure, or the repository; a typed number is refused."""
import json
import re
from pathlib import Path

import pytest
import yaml

from core.eval import deck as D
from core.eval.deck import DeckError, build, repo_counts, to_markdown
from core.eval.gate4 import HeldOutRunner, deck_numbers

ROOT = Path(__file__).resolve().parent.parent
SLIDES = yaml.safe_load((ROOT / "deck" / "slides.yaml").read_text(encoding="utf-8"))
FIGURES = json.loads((ROOT / "deck" / "figures.json").read_text(encoding="utf-8"))
H = "hash-now"
METRICS = {
    "unsupported_claim_rate": {"k": 0, "n": 200, "value": 0.0, "lo": 0.0, "hi": 0.015, "method": "rule_of_three"},
    "grounding_false_block": {"k": 3, "n": 60, "value": 0.05, "lo": 0.017, "hi": 0.137, "method": "wilson"},
    "forbidden_executed": {"k": 0, "n": 90, "value": 0.0, "lo": 0.0, "hi": 0.033, "method": "rule_of_three"},
    "escalation_recall": {"k": 57, "n": 60, "value": 0.95, "lo": 0.863, "hi": 0.983, "method": "wilson"},
    "resolution_rate": {"k": 40, "n": 45, "value": 0.889, "lo": 0.765, "hi": 0.955, "method": "wilson"},
    "pass_k": {"value": 0.8123},
}
RUN = {"run_id": "MR-0001", "config_hash": H, "started_at": "2026-10-31T09:00:00+05:30", "results": {"metrics": METRICS}}
COUNTS = {"playbooks": lambda: 30, "policy_rules": lambda: 24, "arena_levels": lambda: 7}


def make(**kw):
    kw.setdefault("run", RUN)
    kw.setdefault("counts", COUNTS)
    return build(SLIDES, FIGURES, current_hash=H, **kw)


def test_the_slides_file_has_no_digit_outside_a_token():
    texts = [s["title"] for s in SLIDES["slides"]] + [b for s in SLIDES["slides"] for b in s["bullets"]] + [b["title"] for b in SLIDES["backups"]] + [x for b in SLIDES["backups"] for x in b["bullets"]]
    assert len(SLIDES["slides"]) == 8
    for t in texts:
        assert not re.search(r"\d", D.TOKEN.sub("", t)), t


def test_with_a_held_out_run_the_final_deck_builds_and_every_number_in_it_is_in_the_trace():
    deck = make()
    assert deck["problems"] == [] and deck["run_id"] == "MR-0001"
    text = " ".join([s["title"] for s in deck["slides"]] + [b for s in deck["slides"] for b in s["bullets"]])
    for t in deck["trace"]:
        text = text.replace(t["rendered"], "")                                            # remove every number that has a source
    assert not re.search(r"\d", text), text                                              # nothing numeric is left that has none
    tokens = {t["token"] for t in deck["trace"]}
    assert {"fig:subscription_merchants_q2", "repo:playbooks", "repo:policy_rules", "metric:escalation_recall", "metric:pass_k"} <= tokens
    seven = next(s for s in deck["slides"] if s["n"] == "seven")
    assert "57 of 60 (95.0%; 95% interval 86.3% to 98.3%)" in " ".join(seven["bullets"]) and "0.812" in " ".join(seven["bullets"])
    held = [t for t in deck["trace"] if t["token"].startswith("metric:")]
    assert held and all("held-out run MR-0001" in t["source"] for t in held)


def test_without_a_held_out_run_the_final_deck_is_refused_and_the_draft_shows_the_gaps():
    with pytest.raises(DeckError) as e:
        make(run=None)
    assert any("held-out run" in p for p in e.value.problems)
    draft = make(run=None, draft=True)
    assert draft["draft"] and "[PENDING: held-out run]" in to_markdown(draft) and draft["run_id"] is None
    assert any(t["token"] == "fig:subscription_merchants_q2" for t in draft["trace"])        # verified figures still render in a draft


def test_a_run_for_older_code_is_refused():
    old = {**RUN, "config_hash": "hash-before"}
    with pytest.raises(DeckError) as e:
        make(run=old)
    assert any("not the current" in p for p in e.value.problems)


def test_an_unverified_or_secondary_figure_cannot_be_on_a_slide():
    for key in ("avg_sales_employees", "ai_queries", "registered_without_device", "soundbox_launch_date"):
        doc = {"slides": [{"n": "x", "title": f"{{fig:{key}}}", "section": "s", "bullets": []}]}
        with pytest.raises(DeckError) as e:
            build(doc, FIGURES, run=RUN, current_hash=H, counts=COUNTS)
        assert FIGURES["figures"][key]["status"] in e.value.problems[0]


def test_a_number_typed_into_a_slide_is_refused_even_next_to_a_valid_token():
    doc = {"slides": [{"n": "x", "title": "t", "section": "s", "bullets": ["We serve 1.64 crore merchants and {repo:playbooks} playbooks."]}]}
    with pytest.raises(DeckError) as e:
        build(doc, FIGURES, run=RUN, current_hash=H, counts=COUNTS)
    assert "typed into the text" in e.value.problems[0]


@pytest.mark.parametrize("text", ["One merchant, three cases, all finished.", "Get a zero-percent loan.", "A dozen playbooks.", "Twice as fast.",
                                  "ಮೂರು ಪ್ರಕರಣಗಳು ಮುಗಿದಿವೆ.", "Half the cost."])
def test_a_number_written_as_a_word_is_refused_too(text):
    doc = {"slides": [{"n": "x", "title": "t", "section": "s", "bullets": [text]}]}
    with pytest.raises(DeckError) as e:
        build(doc, FIGURES, run=RUN, current_hash=H, counts=COUNTS)
    assert "typed into the text" in e.value.problems[0]


@pytest.mark.parametrize("text", ["One AI teammate that finishes cases.", "Act through one executor.", "Every figure is from one frozen held-out run."])
def test_one_as_an_article_is_not_a_figure(text):
    doc = {"slides": [{"n": "x", "title": "t", "section": "s", "bullets": [text]}]}
    assert build(doc, FIGURES, run=RUN, current_hash=H, counts=COUNTS)["problems"] == []


def test_a_metric_the_run_does_not_have_and_an_unknown_token_are_refused():
    run = {**RUN, "results": {"metrics": {k: v for k, v in METRICS.items() if k != "forbidden_executed"}}}
    with pytest.raises(DeckError) as e:
        make(run=run)
    assert any("no metric 'forbidden_executed'" in p for p in e.value.problems)
    doc = {"slides": [{"n": "x", "title": "{metric:made_up}", "section": "s", "bullets": ["{repo:nothing}", "{fig:nothing}"]}]}
    with pytest.raises(DeckError) as e2:
        build(doc, FIGURES, run=RUN, current_hash=H, counts=COUNTS)
    assert len(e2.value.problems) == 3


def test_the_figure_register_marks_what_was_checked_and_corrects_the_revenue_label():
    f = FIGURES["figures"]
    assert f["subscription_merchants_q2"]["status"] == "VERIFIED" and "1.64" in f["subscription_merchants_q2"]["text"]
    assert "total revenue" in f["total_revenue_q2"]["text"] and "operating revenue" in f["total_revenue_q2"]["note"]       # the Bible's label was wrong
    assert f["ai_queries"]["status"] == "SECONDARY" and f["soundbox_launch_date"]["status"] == "UNVERIFIED"
    assert all(v["status"] in ("VERIFIED", "UNVERIFIED", "SECONDARY") and v["source"] for v in f.values())


def test_real_repository_counts_match_the_repository():
    c = repo_counts()
    assert c["playbooks"]() == len(list((ROOT / "packs" / "paytm_merchant" / "playbooks").glob("*.yaml")))
    from packs.paytm_merchant.policy.rules import default_rules
    assert c["policy_rules"]() == len(default_rules()) and c["arena_levels"]() == 7


def test_the_deck_comes_only_from_a_stored_held_out_run(conn, clock):
    HeldOutRunner(conn, clock).run("heldout", "h-real", lambda: {"code_hash": "c", "metrics": METRICS})
    run = deck_numbers(conn, "h-real")
    deck = build(SLIDES, FIGURES, run=run, current_hash="h-real", counts=COUNTS)
    assert deck["run_id"].startswith("MR-") and not deck["problems"]


def test_the_command_refuses_without_a_run_and_writes_a_draft_on_request(db_path, tmp_path, capsys):
    assert D.main(["--db", str(db_path)]) == 2
    out = tmp_path / "draft.md"
    assert D.main(["--db", str(db_path), "--draft", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "DRAFT" in text and "[PENDING: held-out run]" in text and "Slide 7" in text
