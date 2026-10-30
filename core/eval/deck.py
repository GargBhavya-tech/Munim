"""Deck builder (Build Map #156): every number on a slide traces to a verified Paytm figure, the one held-out run, or the repository. Nothing is typed.

``deck/slides.yaml`` holds slide text with no digits outside ``{token}``s:
  {fig:key}     deck/figures.json, only when the figure is VERIFIED;
  {metric:key}  the held-out run's ``results["metrics"][key]`` for the CURRENT configuration (core/eval/gate4.deck_numbers refuses anything else);
  {repo:key}    a count read from the repository now.
A bare digit or a number word anywhere else is an error, so a number cannot be typed from memory. ``draft=True`` renders the same deck with every missing number shown as a
visible PENDING marker (for layout and wording); the final build refuses until every number has a source, and returns the trace that proves it.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Mapping

import yaml

from core.gate.numbers import extract_numbers

ROOT = Path(__file__).resolve().parent.parent.parent
SLIDES = ROOT / "deck" / "slides.yaml"
FIGURES = ROOT / "deck" / "figures.json"
TOKEN = re.compile(r"\{(fig|metric|repo):([a-z0-9_]+)\}")
# Number words count as typed numbers too. core.gate.numbers reads "three", Hindi and Kannada number words; these English quantity words it leaves alone
# (they are ambiguous in a chat), but on a slide they state a figure. A lone "one" is an article ("one executor"), as in the gate.
QUANTITY_WORDS = re.compile(r"\b(zero|dozens?|twice|thrice|half|double|triple|couple)\b", re.I)


class DeckError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def repo_counts() -> dict[str, Callable[[], int]]:
    def playbooks() -> int:
        return len(list((ROOT / "packs" / "paytm_merchant" / "playbooks").glob("*.yaml")))

    def policy_rules() -> int:
        from packs.paytm_merchant.policy.rules import default_rules
        return len(default_rules())

    def arena_levels() -> int:
        from core.judge.arena import load_levels
        return len(load_levels())
    return {"playbooks": playbooks, "policy_rules": policy_rules, "arena_levels": arena_levels}


def _fmt_metric(v) -> str:
    if isinstance(v, Mapping) and {"k", "n"} <= set(v):
        if not v["n"]:
            return "not measured (no cases)"
        lo, hi = v.get("lo"), v.get("hi")
        return f"{v['k']} of {v['n']}" + (f" ({v['value']:.1%}; 95% interval {lo:.1%} to {hi:.1%})" if lo is not None and hi is not None and v.get("value") is not None else "")
    if isinstance(v, Mapping) and "value" in v:
        return f"{v['value']:.3f}"
    return str(v)


def build(slides_doc: Mapping, figures_doc: Mapping, *, run: Mapping | None, current_hash: str, counts: Mapping[str, Callable[[], int]] | None = None,
          draft: bool = False) -> dict:
    counts = counts if counts is not None else repo_counts()
    figures = figures_doc["figures"]
    problems: list[str] = []
    trace: list[dict] = []
    if run is not None and run.get("config_hash") != current_hash:
        problems.append(f"the held-out run is for configuration {run.get('config_hash')}, not the current {current_hash}")
        run = None
    metrics = (run or {}).get("results", {}).get("metrics", {}) if run else {}

    def resolve(slide: str, kind: str, key: str) -> str:
        if kind == "fig":
            f = figures.get(key)
            if f is None:
                problems.append(f"slide {slide}: unknown figure {key!r}")
                return f"[UNKNOWN FIGURE {key}]"
            if f["status"] != "VERIFIED":
                problems.append(f"slide {slide}: figure {key!r} is {f['status']}, only VERIFIED figures may be on a slide")
                return f"[{f['status']}: {key}]"
            trace.append({"slide": slide, "token": f"fig:{key}", "rendered": f["text"], "source": f["source"]})
            return f["text"]
        if kind == "metric":
            if run is None:
                problems.append(f"slide {slide}: metric {key!r} needs the held-out run for the current configuration")
                return "[PENDING: held-out run]"
            if key not in metrics:
                problems.append(f"slide {slide}: the held-out run has no metric {key!r}")
                return f"[MISSING METRIC {key}]"
            out = _fmt_metric(metrics[key])
            trace.append({"slide": slide, "token": f"metric:{key}", "rendered": out, "source": f"held-out run {run['run_id']} (configuration {run['config_hash']}, {run['started_at']})"})
            return out
        fn = counts.get(key)
        if fn is None:
            problems.append(f"slide {slide}: unknown repository count {key!r}")
            return f"[UNKNOWN COUNT {key}]"
        out = str(fn())
        trace.append({"slide": slide, "token": f"repo:{key}", "rendered": out, "source": "counted from the repository now"})
        return out

    def render(slide: str, text: str) -> str:
        bare = TOKEN.sub("", text)
        if re.search(r"\d", bare) or extract_numbers(bare) or QUANTITY_WORDS.search(bare):
            problems.append(f"slide {slide}: a number is typed into the text: {text!r}")
        return TOKEN.sub(lambda m: resolve(slide, m.group(1), m.group(2)), text)

    out_slides = [{"n": s["n"], "title": render(s["n"], s["title"]), "section": s["section"], "bullets": [render(s["n"], b) for b in s["bullets"]]} for s in slides_doc["slides"]]
    backups = [{"title": render("backup", b["title"]), "bullets": [render("backup", x) for x in b["bullets"]]} for b in slides_doc.get("backups", [])]
    if problems and not draft:
        raise DeckError(problems)
    return {"slides": out_slides, "backups": backups, "trace": trace, "problems": problems, "draft": draft,
            "config_hash": current_hash, "run_id": (run or {}).get("run_id")}


def to_markdown(deck: dict) -> str:
    head = "DRAFT: numbers marked PENDING have no source yet." if deck["draft"] else f"Final: every number traces to the held-out run {deck['run_id']} or a verified source."
    lines = [f"# Munim finals deck ({head})", ""]
    for i, s in enumerate(deck["slides"], 1):
        lines += [f"## Slide {i}: {s['title']}", f"*{s['section']}*", ""] + [f"- {b}" for b in s["bullets"]] + [""]
    lines += ["## Backup slides", ""] + [f"- **{b['title']}**: {' '.join(b['bullets'])}" for b in deck["backups"]] + [""]
    if deck["problems"]:
        lines += ["## Open problems", ""] + [f"- {p}" for p in deck["problems"]] + [""]
    lines += ["## Where every number comes from", ""] + [f"- slide {t['slide']}, `{t['token']}` = {t['rendered']} ({t['source']})" for t in deck["trace"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.eval.deck")
    ap.add_argument("--db", default=str(ROOT / "data" / "munim.db"))
    ap.add_argument("--draft", action="store_true", help="render with PENDING markers instead of refusing")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    from core.eval.gate4 import NoFrozenRun, deck_numbers, freeze_hash
    from core.memory.store import connect
    h = freeze_hash(ROOT)
    run = None
    try:
        if not Path(args.db).is_file():
            raise NoFrozenRun(f"no database at {args.db}, so no held-out run exists")
        try:
            run = deck_numbers(connect(args.db), h)
        except sqlite3.OperationalError:                     # a database that was never initialised has no runs table
            raise NoFrozenRun("the database has no recorded runs")
    except NoFrozenRun as e:
        print(f"no usable held-out run: {e}", file=sys.stderr)
        if not args.draft:
            return 2
    deck = build(yaml.safe_load(SLIDES.read_text(encoding="utf-8")), json.loads(FIGURES.read_text(encoding="utf-8")), run=run, current_hash=h, draft=args.draft)
    text = to_markdown(deck)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
