"""#119 E5: the Indic-entailment-vs-back-translation comparison report. The models are stand-ins; this proves the comparison, not the model quality."""
import json
from pathlib import Path

import pytest

from core.eval import e5
from core.eval.e5 import E5Item, OWN_SET, evaluate, kb_premise_resolver, not_run_report, own_items_from_claims

ROOT = Path(__file__).resolve().parent.parent
CLAIMS = [json.loads(x) for x in (ROOT / "eval" / "frozen" / "claims" / "claims.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def items(n_hi=20, n_kn=20, bad_every=4):
    out = []
    for lang, n in (("hi", n_hi), ("kn", n_kn)):
        for i in range(n):
            out.append(E5Item(f"{lang}-{i}", lang, "premise", f"hyp {i}", grounded=(i % bad_every != 0)))
    return out


oracle = lambda it: it.grounded                       # noqa: E731  sends exactly the grounded replies
paranoid = lambda it: False                           # noqa: E731  blocks everything
lax = lambda it: True                                 # noqa: E731  sends everything


def test_our_own_labelled_hi_and_kn_replies_come_from_the_frozen_set_with_kb_premises():
    own = own_items_from_claims(CLAIMS, kb_premise_resolver(ROOT / "packs" / "paytm_merchant" / "kb"))
    langs = {i.lang for i in own}
    assert langs == {"hi", "kn"}
    for lang in langs:
        sub = [i for i in own if i.lang == lang]
        assert len(sub) >= e5.MIN_OWN_PER_LANG and any(i.grounded for i in sub) and any(not i.grounded for i in sub), lang
    assert all(i.premise_en.strip() and i.hypothesis.strip() for i in own)


def test_a_claim_without_a_knowledge_base_premise_is_skipped_not_guessed():
    res = lambda c: None                              # noqa: E731
    assert own_items_from_claims(CLAIMS, res) == []


def test_exact_false_block_leak_and_agreement_numbers():
    its = items()                                     # per language: 20 items, 5 ungrounded (i % 4 == 0), 15 grounded
    rep = evaluate({OWN_SET: its}, {"back_translation": oracle, "indic_model": paranoid})
    own = rep["sets"][OWN_SET]
    assert own["by_decider"]["back_translation"]["false_block"]["k"] == 0 and own["by_decider"]["back_translation"]["leak"]["k"] == 0
    assert own["by_decider"]["indic_model"]["false_block"] == {"k": 30, "n": 30, "value": 1.0, "lo": 0.9, "hi": 1.0, "method": "rule_of_three"}
    assert own["by_decider"]["indic_model"]["leak"]["k"] == 0
    assert own["agreement"]["k"] == 10 and own["agreement"]["n"] == 40                     # they agree only on the 10 ungrounded replies
    assert own["by_language"]["hi"]["n"] == 20 and own["by_language"]["kn"]["indic_model"]["false_block"]["k"] == 15
    assert rep["status"] == "COMPLETE" and rep["deciders"] == ["back_translation", "indic_model"]


def test_a_leak_is_counted_against_the_checker_that_sent_the_ungrounded_reply():
    rep = evaluate({OWN_SET: items()}, {"a": oracle, "b": lax})
    b = rep["sets"][OWN_SET]["by_decider"]["b"]
    assert b["leak"]["k"] == 10 and b["leak"]["n"] == 10 and b["false_block"]["k"] == 0


def test_a_machine_translated_test_set_alone_is_incomplete_and_says_so():
    rep = evaluate({"indicxnli_test": items(50, 50)}, {"a": oracle, "b": paranoid})
    assert rep["status"] == "INCOMPLETE"
    assert any("hi" in p and "machine-translated" in p for p in rep["problems"]) and any("kn" in p for p in rep["problems"])


def test_too_few_of_our_own_replies_in_one_language_is_incomplete():
    rep = evaluate({"indicxnli_test": items(50, 50), OWN_SET: items(25, 19)}, {"a": oracle, "b": paranoid})
    assert rep["status"] == "INCOMPLETE" and len(rep["problems"]) == 1 and "kn" in rep["problems"][0]


def test_both_sets_are_reported_separately():
    rep = evaluate({"indicxnli_test": items(50, 50), OWN_SET: items(25, 25)}, {"a": oracle, "b": paranoid})
    assert set(rep["sets"]) == {"indicxnli_test", OWN_SET} and rep["status"] == "COMPLETE"
    assert rep["sets"]["indicxnli_test"]["n"] == 100 and rep["sets"][OWN_SET]["n"] == 50


def test_one_checker_is_not_a_comparison():
    with pytest.raises(ValueError):
        evaluate({OWN_SET: items()}, {"only": oracle})


def test_until_the_model_exists_the_report_says_not_run_and_carries_no_numbers(tmp_path):
    rep = not_run_report("no fine-tuned weights in models/indic_nli")
    assert rep["status"] == "NOT RUN" and rep["sets"] == {} and rep["deciders"] == []
    e5.write(rep, tmp_path / "out" / "e5.json")
    assert json.loads((tmp_path / "out" / "e5.json").read_text(encoding="utf-8"))["status"] == "NOT RUN"


def test_the_training_script_dry_run_downloads_nothing_and_a_real_run_needs_explicit_consent():
    import os
    import subprocess
    import sys
    env = {**os.environ, "PYTHONUTF8": "1"}
    dry = subprocess.run([sys.executable, "scripts/train_indic_nli.py", "--dry-run"], cwd=ROOT, capture_output=True, text=True, env=env)
    plan = json.loads(dry.stdout)
    assert dry.returncode == 0 and plan["labels"] == ["entailment", "neutral", "contradiction"] and "a GPU" in plan["needs"]
    refused = subprocess.run([sys.executable, "scripts/train_indic_nli.py", "--train-file", "a.jsonl", "--eval-file", "b.jsonl"], cwd=ROOT, capture_output=True, text=True, env=env)
    assert refused.returncode == 2 and "refusing to run" in refused.stderr                   # no --download-ok: nothing is imported, nothing downloaded
