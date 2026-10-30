"""E5 (Build Map #119): an Indic entailment model beside back-translation, with a report that compares them honestly.

The question: for a rendered Hindi or Kannada reply, does a fine-tuned cross-encoder (MuRIL or XLM-R, trained on IndicXNLI) say the same thing as the gate's
back-translation check, and does it block fewer true replies? Both are judged on the SAME items and reported with 95% intervals:
  * false-block rate: grounded (true) replies that were blocked;
  * leak rate: ungrounded (false) replies that were sent;
  * agreement: how often the two checks make the same decision.
The watch-out is built in: IndicXNLI's test split is machine-translated, so a report is COMPLETE only if it also covers our own labelled hi and kn replies
(the frozen claim set, premises from the knowledge base). A report without that set is marked INCOMPLETE and never says it passed.

What decides is injected (``deciders``: name -> callable(item) -> True when the reply would be SENT), so this module owns the comparison, not the models.
Training is NOT done here: see scripts/train_indic_nli.py. Until weights exist, the report states NOT RUN.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from core.eval.metrics import rate

OWN_SET = "own_labelled"
LANGS = ("hi", "kn")
MIN_OWN_PER_LANG = 20            # fewer than this cannot say anything about a language


@dataclass(frozen=True)
class E5Item:
    item_id: str
    lang: str                    # hi | kn
    premise_en: str              # the verified English source
    hypothesis: str              # the rendered Indic reply
    grounded: bool               # True: the source supports it and it should be sent


def own_items_from_claims(claims: Iterable[Mapping], premise_for: Callable[[Mapping], str | None]) -> list[E5Item]:
    """Our labelled hi/kn replies: the frozen claim rows whose truth is a knowledge-base section. Rows with no premise are skipped, not guessed."""
    out = []
    for c in claims:
        if c["lang"] not in LANGS:
            continue
        premise = premise_for(c)
        if premise:
            out.append(E5Item(c["id"], c["lang"], premise, c["text"], c["label"] == "true"))
    return out


def kb_premise_resolver(kb_dir: Path | str) -> Callable[[Mapping], str | None]:
    """The text of the KB section a claim's truth points at (kind 'kb' only; tool results need a live read and are skipped)."""
    from core.memory.knowledge import load_kb_dir
    docs = {d.doc_id: d for d in load_kb_dir(Path(kb_dir))}

    def resolve(c: Mapping) -> str | None:
        t = c.get("truth") or {}
        if t.get("kind") != "kb":
            return None
        doc = docs.get(t.get("doc"))
        if doc is None:
            return None
        sec = next((s for s in doc.sections if s.section_id == t.get("section")), None)
        return sec.text if sec else None
    return resolve


def _confusion(items: list[E5Item], sent: dict[str, bool]) -> dict:
    grounded = [i for i in items if i.grounded]
    ungrounded = [i for i in items if not i.grounded]
    fb = sum(1 for i in grounded if not sent[i.item_id])
    lk = sum(1 for i in ungrounded if sent[i.item_id])
    return {"false_block": rate(fb, len(grounded)).as_dict(), "leak": rate(lk, len(ungrounded)).as_dict(),
            "n": len(items), "grounded": len(grounded), "ungrounded": len(ungrounded)}


def evaluate(sets: Mapping[str, list[E5Item]], deciders: Mapping[str, Callable[[E5Item], bool]]) -> dict:
    """sets: set name -> items (e.g. {'indicxnli_test': [...], 'own_labelled': [...]}). deciders: exactly the checks to compare."""
    if len(deciders) < 2:
        raise ValueError("E5 compares at least two checks (back-translation and the Indic model)")
    report: dict = {"sets": {}, "deciders": sorted(deciders), "status": "COMPLETE", "problems": []}
    for name, items in sets.items():
        decisions = {d: {i.item_id: bool(fn(i)) for i in items} for d, fn in deciders.items()}
        per: dict = {"n": len(items), "by_decider": {}, "by_language": {}}
        for d in deciders:
            per["by_decider"][d] = _confusion(items, decisions[d])
        names = sorted(deciders)
        same = sum(1 for i in items if len({decisions[d][i.item_id] for d in names}) == 1)
        per["agreement"] = rate(same, len(items)).as_dict()
        for lang in LANGS:
            sub = [i for i in items if i.lang == lang]
            per["by_language"][lang] = {"n": len(sub), **{d: _confusion(sub, decisions[d]) for d in names}} if sub else {"n": 0}
        report["sets"][name] = per
    own = sets.get(OWN_SET, [])
    for lang in LANGS:
        n = sum(1 for i in own if i.lang == lang)
        if n < MIN_OWN_PER_LANG:
            report["status"] = "INCOMPLETE"
            report["problems"].append(f"only {n} of our own labelled {lang} replies (need {MIN_OWN_PER_LANG}); a machine-translated test set alone is not enough")
    return report


def not_run_report(reason: str) -> dict:
    """What is written when the Indic model does not exist yet: a clear statement, never numbers."""
    return {"status": "NOT RUN", "reason": reason, "sets": {}, "deciders": [], "problems": [reason]}


def write(report: dict, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
