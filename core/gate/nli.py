"""English entailment (NLI) for the grounding gate.

PREMISE = the SOURCE (tool result text, KB chunk, policy rule), HYPOTHESIS = the CLAIM. Feeding them the other way round
changes the label (a short claim never entails a longer source), so the order lives in exactly one place: ``_pairs``.
Labels are read from the model's own ``config.id2label`` and normalised; nothing about label order is hard-coded.
Only ENTAILMENT passes.
"""
from __future__ import annotations

from typing import Sequence

ENTAILMENT, NEUTRAL, CONTRADICTION = "ENTAILMENT", "NEUTRAL", "CONTRADICTION"
LABELS = (ENTAILMENT, NEUTRAL, CONTRADICTION)
DEFAULT_NLI_MODEL = "cross-encoder/nli-deberta-v3-base"


def normalise_label(raw: str) -> str:
    """Map a model-specific label name ('entailment', 'LABEL_0'-free names, 'contradiction') onto our three labels."""
    s = raw.strip().upper()
    for canon in LABELS:
        if s == canon or s.startswith(canon[:6]):  # ENTAIL / NEUTRA / CONTRA
            return canon
    raise ValueError(f"unrecognised NLI label {raw!r}")


class EntailmentModel:
    def __init__(self, model_name: str = DEFAULT_NLI_MODEL, device: str = "cpu", max_length: int = 512):
        self.name = model_name
        self.device = device
        self.max_length = max_length
        self._tok = None
        self._model = None
        self.id2label: dict[int, str] = {}

    def _load(self):
        if self._model is None:
            import torch  # noqa: F401
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            self._tok = AutoTokenizer.from_pretrained(self.name)
            self._model = AutoModelForSequenceClassification.from_pretrained(self.name).to(self.device).eval()
            # the mapping comes from the model's own config, never from an assumed order
            self._set_labels(self._model.config.id2label)
        return self._model

    def _set_labels(self, raw_id2label: dict) -> None:
        self.id2label = {int(i): normalise_label(l) for i, l in raw_id2label.items()}
        if set(self.id2label.values()) != set(LABELS) or len(self.id2label) != 3:
            raise ValueError(f"{self.name}: id2label {raw_id2label} does not cover {LABELS}")

    @staticmethod
    def _pairs(items: Sequence[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """items are (source, claim). Returns (premises, hypotheses): the source is the premise."""
        return [s for s, _ in items], [c for _, c in items]

    def _logits(self, premises: list[str], hypotheses: list[str]):
        """Run the classifier. Kept separate so tests can observe exactly what is fed as premise / hypothesis."""
        import torch
        model = self._load()
        enc = self._tok(premises, hypotheses, padding=True, truncation=True, max_length=self.max_length, return_tensors="pt").to(self.device)
        with torch.no_grad():
            return model(**enc).logits

    def label_batch(self, pairs: Sequence[tuple[str, str]]) -> list[tuple[str, dict[str, float]]]:
        """Label several (source, claim) pairs. Returns [(label, {ENTAILMENT: p, NEUTRAL: p, CONTRADICTION: p})]."""
        if not pairs:
            return []
        import torch
        self._load()
        premises, hypotheses = self._pairs(pairs)
        probs = torch.softmax(self._logits(premises, hypotheses), dim=-1)
        out = []
        for row in probs:
            d = {self.id2label[i]: float(row[i]) for i in range(row.shape[0])}
            out.append((max(d, key=d.get), d))
        return out

    def label(self, source: str, claim: str) -> tuple[str, dict[str, float]]:
        return self.label_batch([(source, claim)])[0]

    def entails(self, source: str, claim: str) -> bool:
        return self.label(source, claim)[0] == ENTAILMENT

    def entails_batch(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        return [lab == ENTAILMENT for lab, _ in self.label_batch(pairs)]
