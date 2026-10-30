"""Per-language supported bar (Build Map #126, E12): a language is switched on only by a passing report, never by editing a flag.

The bar for a language: speech-to-text works, text-to-speech works, translation works (each a recorded check with its evidence), and on a labelled set of at
least 30 replies the grounding gate wrongly blocks at most 5% of the grounded ones and sends none of the ungrounded ones. A language with no labelled set has
no measurement, so it cannot pass: enabling it is refused, not waved through. The baseline languages shipped before the bar existed stay on and are reported
as having no report yet; they are never listed as proven.

What plays the labelled replies through the gate is injected (``send_decision``), so this module owns the rule, not the models.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "languages.yaml"
LABELLED_DIR = Path(__file__).resolve().parent.parent.parent / "eval" / "languages"
CHECKS = ("stt", "tts", "translate")


class LanguageNotReady(RuntimeError):
    pass


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def candidates(cfg: dict | None = None) -> dict[str, str]:
    cfg = cfg or load_config()
    return {c["code"]: c["name"] for c in cfg["candidates"]}


@dataclass(frozen=True)
class LabelledReply:
    reply_id: str
    text: str
    grounded: bool             # True: the reply is supported by its source and should be sent. False: it is not and must be blocked.


def load_labelled(code: str, directory: Path | str = LABELLED_DIR) -> list[LabelledReply]:
    """eval/languages/<code>.jsonl, one {"id", "text", "grounded"} per line. A missing file is an empty set."""
    f = Path(directory) / f"{code}.jsonl"
    if not f.is_file():
        return []
    return [LabelledReply(str(d["id"]), str(d["text"]), bool(d["grounded"])) for d in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())]


def evaluate(code: str, checks: dict[str, str | None], labelled: Iterable[LabelledReply], send_decision: Callable[[LabelledReply], bool], cfg: dict | None = None) -> dict:
    """The report for one language. ``checks`` maps stt/tts/translate to the evidence reference of a passing check (None or missing = not passed).
    ``send_decision(reply)`` is True when the gate would SEND the reply."""
    cfg = cfg or load_config()
    names = candidates(cfg)
    if code not in names:
        raise LanguageNotReady(f"{code} is not a candidate language")
    bar = cfg["bar"]
    replies = list(labelled)
    grounded = [r for r in replies if r.grounded]
    ungrounded = [r for r in replies if not r.grounded]
    sent = {r.reply_id: bool(send_decision(r)) for r in replies}
    false_blocks = sum(1 for r in grounded if not sent[r.reply_id])
    leaks = sum(1 for r in ungrounded if sent[r.reply_id])
    rate = (false_blocks / len(grounded)) if grounded else None
    why: list[str] = []
    for c in CHECKS:
        if not checks.get(c):
            why.append(f"{c} has no passing check")
    if len(replies) < bar["min_labelled_replies"]:
        why.append(f"only {len(replies)} labelled replies (need {bar['min_labelled_replies']}); the false-block rate cannot be measured")
    elif not grounded or not ungrounded:
        why.append("the labelled set needs both grounded and ungrounded replies")
    else:
        if rate > bar["max_false_block_rate"]:
            why.append(f"false-block rate {rate:.1%} is above {bar['max_false_block_rate']:.0%} ({false_blocks} of {len(grounded)} grounded replies blocked)")
        if leaks > bar["max_leaks"]:
            why.append(f"{leaks} ungrounded replies were sent (allowed {bar['max_leaks']})")
    return {"code": code, "name": names[code], "passes": not why, "why_not": why, "checks": {c: checks.get(c) for c in CHECKS},
            "labelled": {"n": len(replies), "grounded": len(grounded), "ungrounded": len(ungrounded)},
            "false_block_rate": rate, "false_blocks": false_blocks, "leaks": leaks, "bar": dict(bar)}


class LanguageRegistry:
    """Which languages are on. The baseline is always on; every other language is on only because a passing report was stored for it."""

    def __init__(self, conn: sqlite3.Connection, clock, ledger, cfg: dict | None = None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        self.cfg = cfg or load_config()

    def _report(self, code: str) -> dict | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (f"lang_report:{code}",)).fetchone()
        return json.loads(r[0]) if r else None

    def enable(self, report: dict) -> None:
        """Switch a language on. Refused unless the report passes and its labelled set is the one the bar requires."""
        code = report["code"]
        if code in self.cfg["baseline"]:
            return
        if not report.get("passes") or report["labelled"]["n"] < self.cfg["bar"]["min_labelled_replies"]:
            raise LanguageNotReady(f"{code} does not meet the supported bar: " + "; ".join(report.get("why_not") or ["no passing report"]))
        stamped = {**report, "enabled_at": self.clock.now_iso()}
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (f"lang_report:{code}", json.dumps(stamped, sort_keys=True)))
        self.ledger.append(kind="STATE", actor="SYSTEM", payload={"language_enabled": code, "false_block_rate": report["false_block_rate"], "labelled": report["labelled"]})

    def disable(self, code: str) -> None:
        if code in self.cfg["baseline"]:
            raise LanguageNotReady(f"{code} is a baseline language")
        self.conn.execute("DELETE FROM meta WHERE key = ?", (f"lang_report:{code}",))
        self.ledger.append(kind="STATE", actor="SYSTEM", payload={"language_disabled": code})

    def is_enabled(self, code: str) -> bool:
        return code in self.cfg["baseline"] or self._report(code) is not None

    def status(self) -> list[dict]:
        out = []
        for code, name in candidates(self.cfg).items():
            rep = self._report(code)
            base = code in self.cfg["baseline"]
            out.append({"code": code, "name": name, "enabled": base or rep is not None,
                        "report": "none yet (baseline language)" if base and rep is None else ("passing" if rep else "none"), "report_detail": rep})
        for code in self.cfg["baseline"]:
            if code not in candidates(self.cfg):
                out.append({"code": code, "name": code, "enabled": True, "report": "none yet (baseline language)", "report_detail": None})
        return out
