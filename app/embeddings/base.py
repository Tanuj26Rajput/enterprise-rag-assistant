"""The embedding provider interface.

Providers are interchangeable from the indexing service's point of view, but
they are *not* interchangeable for an existing collection: a collection's
vectors all have to come from one model at one width. Every provider therefore
advertises its ``spec``, and the indexing service refuses to write into a
collection whose recorded model and dimension do not match it.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.embeddings.registry import EmbeddingModelSpec

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """An embedding call failed in a way the caller must handle."""


class EmbeddingDimensionMismatch(EmbeddingError):
    """A provider returned vectors of an unexpected width.

    This is the check that makes a declared dimension safe to trust: an
    operator vouching for an unregistered model is believed only until the
    first real response contradicts them, and then nothing is indexed.
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors."""

    @property
    def spec(self) -> EmbeddingModelSpec:
        """The model being used and the width of the vectors it returns."""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages for storage. Order matches the input."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query.

        Separate from :meth:`embed_documents` because asymmetric models
        prepend different instructions to each side, and because a query is
        always a batch of one.
        """
        ...


def verify_dimensions(vectors: list[list[float]], spec: EmbeddingModelSpec) -> list[list[float]]:
    """Check every vector's width before it reaches the store.

    Chroma would reject a mismatched vector anyway, but it does so per
    operation and with a message about the collection rather than about the
    model. Catching it here names the actual cause.
    """
    for index, vector in enumerate(vectors):
        if len(vector) != spec.dimensions:
            raise EmbeddingDimensionMismatch(
                f"{spec.provider}/{spec.model} returned a {len(vector)}-dimensional vector "
                f"at position {index}, but is configured as {spec.dimensions}-dimensional. "
                "Nothing was indexed. Correct EMBEDDING_MODEL / EMBEDDING_DIMENSIONS, or "
                "point the deployment at a provider that actually serves this model."
            )
    return vectors
