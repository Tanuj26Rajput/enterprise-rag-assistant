"""A deterministic, offline embedding provider.

Exists so the ingestion and retrieval paths can be developed and tested
without an API key, a network, or a bill. The vectors are a hashing trick -
reproducible and nearest-neighbour-searchable, so exact and near-duplicate
text retrieves correctly, which is enough to test indexing, filtering and
deletion. They carry no semantics: "car" and "automobile" are unrelated here.

It is refused outside development and test by Settings validation, because a
production collection built on these vectors would retrieve nothing useful
and there would be no error to notice.
"""

from __future__ import annotations

import hashlib
import math
import re

from app.embeddings.registry import EmbeddingModelSpec

_TOKEN_RE = re.compile(r"\w+")


class HashingEmbeddingProvider:
    """Hashes tokens into a fixed-width bag-of-words vector."""

    def __init__(self, spec: EmbeddingModelSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        dimensions = self._spec.dimensions
        vector = [0.0] * dimensions
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % dimensions
            # The sign bit spreads tokens that collide in the same bucket, so
            # two different words are less likely to reinforce one another.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        # L2-normalised, so cosine distance behaves and an empty string still
        # yields a valid (zero) vector rather than a division by zero.
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector
