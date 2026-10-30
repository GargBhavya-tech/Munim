"""The fixed phrases Munim may say without a source claim (packs/paytm_merchant/phrases.yaml). Looked up by id, never reworded."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

PHRASES_PATH = Path(__file__).resolve().parent.parent.parent / "packs" / "paytm_merchant" / "phrases.yaml"
LANGUAGES = ("en-IN", "hi-IN", "hinglish", "kn-IN")
KINDS = ("conversational", "compliance", "control")


def canon(text: str) -> str:
    """Comparison form: NFC, lower-case, punctuation and whitespace collapsed. A phrase is recognised by this, not by exact bytes."""
    t = unicodedata.normalize("NFC", text).lower()
    return re.sub(r"[\s\W_]+", " ", t).strip()


class Phrases:
    def __init__(self, data: dict | None = None):
        d = data if data is not None else _load(str(PHRASES_PATH))
        self._p = d["phrases"]
        self.review_status = d.get("review_status", "UNKNOWN")
        for pid, p in self._p.items():
            if p["kind"] not in KINDS or any(not p.get(l) for l in LANGUAGES):
                raise ValueError(f"phrase {pid!r} needs a kind in {KINDS} and a text in every language {LANGUAGES}")
        self._canon = {canon(p[l]) for p in self._p.values() for l in LANGUAGES}

    def text(self, pid: str, language: str) -> str:
        return self._p[pid][language if language in LANGUAGES else "en-IN"]

    def has(self, pid: str) -> bool:
        return pid in self._p

    def ids(self, kind: str | None = None) -> tuple[str, ...]:
        return tuple(pid for pid, p in self._p.items() if kind is None or p["kind"] == kind)

    def kind(self, pid: str) -> str:
        return self._p[pid]["kind"]

    def is_phrase_text(self, text: str) -> bool:
        """True when ``text`` is exactly one of the fixed phrases (any language), ignoring case and punctuation."""
        return canon(text) in self._canon


@lru_cache(maxsize=2)
def _load(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
