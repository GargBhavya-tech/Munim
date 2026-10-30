"""Sentence embedders for the knowledge layer.

``Embedder`` is the protocol retrieval depends on. ``SentenceTransformerEmbedder`` is the real multilingual model
(Hindi / Kannada / Hinglish / English); ``HashEmbedder`` is a tiny deterministic test double that needs no model download.
Vectors are L2-normalised float32, so cosine similarity is a plain dot product.
"""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

# Chosen by measurement on the labelled set in tests/test_retrieval.py (see the engineer report for the numbers).
DEFAULT_MODEL = "intfloat/multilingual-e5-small"


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: Sequence[str], kind: str = "passage") -> np.ndarray:
        """Return an (n, dim) float32 array of unit vectors. ``kind`` is "query" or "passage"."""
        ...


def _normalise(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def word_tokens(text: str) -> list[str]:
    """Lower-cased word tokens; letters, digits and combining marks (matras) stay inside a word."""
    out = "".join(c if (c.isalnum() or unicodedata.category(c).startswith("M")) else " " for c in text.lower())
    return out.split()


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder (word unigrams + character trigrams). Test double only."""

    def __init__(self, dim: int = 256):
        self.name = f"hash-{dim}"
        self.dim = dim

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = hashlib.sha256(feature.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "big") % self.dim, 1.0 if h[4] & 1 else -1.0

    def encode(self, texts: Sequence[str], kind: str = "passage") -> np.ndarray:
        m = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in word_tokens(t):
                feats = [w] + ([w[j:j + 3] for j in range(len(w) - 2)] if len(w) > 3 else [])
                for f in feats:
                    b, s = self._bucket(f)
                    m[i, b] += s
        return _normalise(m)


class SentenceTransformerEmbedder:
    """Real multilingual embedder. The model is loaded lazily on first ``encode`` (first call is slow: download + load)."""

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "cpu"):
        self.name = model_name
        self.device = device
        self._model = None
        self.dim = 0

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.name, device=self.device)
            self.dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def _prefix(self, kind: str) -> str:
        if "e5" in self.name.lower():  # e5 models are trained with these prefixes
            return "query: " if kind == "query" else "passage: "
        return ""

    def encode(self, texts: Sequence[str], kind: str = "passage") -> np.ndarray:
        model = self._load()
        p = self._prefix(kind)
        vecs = model.encode([p + t for t in texts], convert_to_numpy=True, normalize_embeddings=False, show_progress_bar=False)
        return _normalise(vecs)


