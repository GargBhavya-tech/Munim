"""Test doubles for the grounding gate. Not collected as tests (the file name does not match python_files)."""
import json
import re

from core.llm.client import LLMClient
from core.llm.sarvam_http import ChatResult

STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "for", "in", "on", "at", "and", "or", "it", "this", "that", "be", "by", "with"}


def words(text):
    return {w for w in re.findall(r"[a-z0-9₹]+(?:\.[0-9]+)?", text.lower()) if w not in STOP}


class FakeNLI:
    """Premise entails the hypothesis when every content word of the hypothesis is in the premise. ``contradict`` is a list of
    (premise substring, hypothesis substring) pairs that label CONTRADICTION. ``calls`` records (premise, hypothesis) in the order given."""

    def __init__(self, contradict=(), neutral=()):
        self.contradict, self.neutral, self.calls = list(contradict), list(neutral), []

    def label_batch(self, pairs):
        out = []
        for premise, hyp in pairs:
            self.calls.append((premise, hyp))
            if any(p in premise and h in hyp for p, h in self.contradict):
                lab = "CONTRADICTION"
            elif any(n in hyp for n in self.neutral):
                lab = "NEUTRAL"
            else:
                lab = "ENTAILMENT" if words(hyp) <= words(premise) else "NEUTRAL"
            out.append((lab, {"ENTAILMENT": float(lab == "ENTAILMENT"), "NEUTRAL": float(lab == "NEUTRAL"), "CONTRADICTION": float(lab == "CONTRADICTION")}))
        return out


class FakeTranslator:
    """Maps a rendered sentence to its English meaning; unknown text comes back unchanged."""

    def __init__(self, table=None):
        self.table, self.calls = dict(table or {}), []

    def to_english(self, text, language):
        self.calls.append((text, language))
        return self.table.get(text, text)


class ScriptedProvider:
    """A chat provider that answers from a list; each item is a dict (JSON-encoded) or a string."""
    name, model_id = "sarvam", "sarvam-105b"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, *, response_format=None, **kw):
        self.seen.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return ChatResult(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False), {"prompt_tokens": 5, "completion_tokens": 5}, {})


def llm(replies, ledger=None):
    prov = ScriptedProvider(replies)
    return LLMClient(prov, None, ledger, sleep=lambda s: None), prov
