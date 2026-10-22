"""Embedding providers and the model/dimension registry."""

from app.embeddings.base import (
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingProvider,
    verify_dimensions,
)
from app.embeddings.factory import build_embedding_provider, get_embedding_provider
from app.embeddings.hashing import HashingEmbeddingProvider
from app.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.embeddings.registry import (
    KNOWN_MODELS,
    EmbeddingConfigurationError,
    EmbeddingModelSpec,
    resolve_spec,
)

__all__ = [
    "KNOWN_MODELS",
    "EmbeddingConfigurationError",
    "EmbeddingDimensionMismatch",
    "EmbeddingError",
    "EmbeddingModelSpec",
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "build_embedding_provider",
    "get_embedding_provider",
    "resolve_spec",
    "verify_dimensions",
]
