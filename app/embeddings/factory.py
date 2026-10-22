"""Builds the configured embedding provider."""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.embeddings.base import EmbeddingProvider
from app.embeddings.hashing import HashingEmbeddingProvider
from app.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.embeddings.registry import EmbeddingConfigurationError, resolve_spec

logger = logging.getLogger(__name__)

#: OpenRouter speaks the OpenAI wire format, so it needs no separate client -
#: only a different base URL and a registry entry vouching for the model.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """Construct the provider named by configuration.

    Raises :class:`EmbeddingConfigurationError` rather than returning a
    provider whose vector width is unknown.
    """
    settings = settings or get_settings()
    spec = settings.embedding_spec

    if spec.provider == "hash":
        return HashingEmbeddingProvider(spec)

    api_key = settings.embedding_api_key_value
    if not api_key:
        raise EmbeddingConfigurationError(
            f"EMBEDDING_PROVIDER={spec.provider} needs an API key: set EMBEDDING_API_KEY "
            "(or OPENAI_API_KEY for the openai provider)."
        )

    base_url = settings.embedding_base_url
    if base_url is None and spec.provider == "openrouter":
        base_url = OPENROUTER_BASE_URL

    logger.info(
        "Embedding provider: %s/%s at %d dimensions",
        spec.provider,
        spec.model,
        spec.dimensions,
    )
    return OpenAIEmbeddingProvider(
        spec,
        api_key=api_key,
        base_url=base_url,
        timeout=float(settings.embedding_request_timeout),
        max_attempts=settings.embedding_max_attempts,
        batch_size=settings.embedding_batch_size,
    )


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    """Process-wide provider, so the HTTP client and its pool are reused."""
    return build_embedding_provider()


__all__ = [
    "EmbeddingConfigurationError",
    "build_embedding_provider",
    "get_embedding_provider",
    "resolve_spec",
]
