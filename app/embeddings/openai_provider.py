"""Embeddings over an OpenAI-compatible HTTP API.

One class serves both OpenAI and OpenRouter because the wire format is the
same; what differs is how much the registry is willing to assume about the
model, which is settled before this class is constructed.
"""

from __future__ import annotations

import logging
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

from app.core.retry import retry_async
from app.embeddings.base import EmbeddingError, verify_dimensions
from app.embeddings.registry import EmbeddingModelSpec

logger = logging.getLogger(__name__)

#: Failures a later attempt could plausibly survive. Deliberately excludes
#: AuthenticationError (a bad key stays bad), BadRequestError (a malformed
#: request stays malformed) and NotFoundError - which is how an OpenRouter
#: model with no embeddings endpoint announces itself, and is a configuration
#: error to surface immediately, not to retry sixty times.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
)


class OpenAIEmbeddingProvider:
    """Calls an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        spec: EmbeddingModelSpec,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_attempts: int = 3,
        batch_size: int = 64,
    ) -> None:
        self._spec = spec
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        # Batched because a whole document's chunks can exceed the per-request
        # input limit, and because a failed batch retries less work.
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed(batch))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]

    async def _embed(self, batch: list[str]) -> list[list[float]]:
        kwargs: dict[str, Any] = {"model": self._spec.model, "input": batch}
        # Only sent when it means something. A model that ignores the argument
        # would otherwise return its native width while we believed otherwise;
        # the registry has already refused that combination, and
        # verify_dimensions below is the backstop.
        if self._spec.supports_dimension_override:
            kwargs["dimensions"] = self._spec.dimensions

        async def call() -> list[list[float]]:
            response = await self._client.embeddings.create(**kwargs)
            # The API does not promise ordering, and a reordered batch would
            # attach every vector to the wrong chunk - silent, and invisible
            # until retrieval quality is inexplicably poor.
            ordered = sorted(response.data, key=lambda item: item.index)
            return [list(item.embedding) for item in ordered]

        try:
            vectors = await retry_async(
                call,
                attempts=self._max_attempts,
                retry_on=TRANSIENT_ERRORS,
                description=f"embedding batch of {len(batch)} via {self._spec.provider}",
            )
        except NotFoundError as exc:
            raise EmbeddingError(
                f"{self._spec.provider} has no embeddings endpoint for model "
                f"{self._spec.model!r}. Routers that front chat models commonly do not serve "
                "embeddings for them; configure a dedicated embedding model."
            ) from exc
        except AuthenticationError as exc:
            raise EmbeddingError(f"The {self._spec.provider} credentials were rejected.") from exc
        except BadRequestError as exc:
            raise EmbeddingError(
                f"{self._spec.provider} rejected the embedding request for "
                f"{self._spec.model!r}: {exc}"
            ) from exc
        except TRANSIENT_ERRORS as exc:
            raise EmbeddingError(
                f"{self._spec.provider} was unreachable after {self._max_attempts} attempts."
            ) from exc

        if len(vectors) != len(batch):
            raise EmbeddingError(f"Requested {len(batch)} embeddings but received {len(vectors)}.")
        return verify_dimensions(vectors, self._spec)
