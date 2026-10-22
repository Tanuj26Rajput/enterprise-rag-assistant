"""What each embedding model actually returns, and which providers serve it.

The vector width is not a preference, it is a property of the model. A Chroma
collection is created with one width and rejects anything else, so a wrong
value here surfaces as a mass of failed documents long after the collection
was created. This module is therefore the single place where a model is turned
into a dimension, and it refuses to guess.

The OpenRouter case is the reason the registry is explicit rather than
permissive. OpenRouter is a router for *chat completions*; it fronts hundreds
of models, and the overwhelming majority of them expose no embeddings endpoint
at all. "The chat model works, so the embedding model will too" is false, and
discovering it at index time means a half-populated collection. An OpenRouter
model is therefore only usable here when it has been named explicitly, with
its dimension stated, by whoever configured the deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EmbeddingProviderName = Literal["openai", "openrouter", "hash"]


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """A model, the provider serving it, and the width of its vectors."""

    provider: EmbeddingProviderName
    model: str
    dimensions: int

    #: True for models that accept a ``dimensions`` argument and return a
    #: correctly re-normalised shorter vector (OpenAI's v3 family uses
    #: Matryoshka representation learning for this). For every other model a
    #: requested override is a configuration error, not a truncation request.
    supports_dimension_override: bool = False

    #: Largest vector the model can return, used to validate an override.
    max_dimensions: int | None = None


#: Models we know the shape of. Keyed by ``(provider, model)``.
KNOWN_MODELS: dict[tuple[str, str], EmbeddingModelSpec] = {
    ("openai", "text-embedding-3-small"): EmbeddingModelSpec(
        provider="openai",
        model="text-embedding-3-small",
        dimensions=1536,
        supports_dimension_override=True,
        max_dimensions=1536,
    ),
    ("openai", "text-embedding-3-large"): EmbeddingModelSpec(
        provider="openai",
        model="text-embedding-3-large",
        dimensions=3072,
        supports_dimension_override=True,
        max_dimensions=3072,
    ),
    # Legacy. No dimensions parameter: the API ignores it rather than failing,
    # which is precisely the silent-wrong-width case this registry exists for.
    ("openai", "text-embedding-ada-002"): EmbeddingModelSpec(
        provider="openai",
        model="text-embedding-ada-002",
        dimensions=1536,
        supports_dimension_override=False,
    ),
    # Deterministic, offline, no API key. Development and tests only - the
    # vectors carry no semantics, so retrieval quality is meaningless.
    ("hash", "hash-256"): EmbeddingModelSpec(provider="hash", model="hash-256", dimensions=256),
}


class EmbeddingConfigurationError(RuntimeError):
    """The configured provider/model/dimension combination cannot be used.

    Raised at startup and at collection creation, never mid-index: the point
    is to fail before any vectors exist, not after half of them do.
    """


def resolve_spec(
    provider: str,
    model: str,
    *,
    requested_dimensions: int | None = None,
    declared_dimensions: int | None = None,
) -> EmbeddingModelSpec:
    """Resolve a provider/model pair to a spec, or explain why it cannot be.

    ``requested_dimensions`` is an operator's explicit request to shorten a
    model that supports it. ``declared_dimensions`` is an operator vouching
    for the width of a model this registry does not know - the only way an
    OpenRouter model becomes usable.
    """
    known = KNOWN_MODELS.get((provider, model))

    if known is None:
        return _resolve_unknown(provider, model, declared_dimensions)

    if declared_dimensions is not None and declared_dimensions != known.dimensions:
        if not known.supports_dimension_override:
            raise EmbeddingConfigurationError(
                f"{provider}/{model} returns {known.dimensions}-dimensional vectors; "
                f"EMBEDDING_DIMENSIONS={declared_dimensions} contradicts that. Remove the "
                f"override, or choose a model that supports one."
            )
        requested_dimensions = requested_dimensions or declared_dimensions

    if requested_dimensions is None or requested_dimensions == known.dimensions:
        return known

    if not known.supports_dimension_override:
        raise EmbeddingConfigurationError(
            f"{provider}/{model} always returns {known.dimensions} dimensions and ignores a "
            f"dimensions argument, so a request for {requested_dimensions} would silently "
            f"produce {known.dimensions}-wide vectors."
        )
    if requested_dimensions > (known.max_dimensions or known.dimensions):
        raise EmbeddingConfigurationError(
            f"{provider}/{model} returns at most {known.max_dimensions} dimensions; "
            f"{requested_dimensions} was requested."
        )
    return EmbeddingModelSpec(
        provider=known.provider,
        model=known.model,
        dimensions=requested_dimensions,
        supports_dimension_override=True,
        max_dimensions=known.max_dimensions,
    )


def _resolve_unknown(
    provider: str, model: str, declared_dimensions: int | None
) -> EmbeddingModelSpec:
    """Handle a model the registry has never heard of."""
    if provider == "openrouter":
        if declared_dimensions is None:
            raise EmbeddingConfigurationError(
                f"OpenRouter model {model!r} has no registered embedding dimension. "
                "OpenRouter routes chat completions; most of the models it fronts expose no "
                "embeddings endpoint, and the ones that do are not interchangeable. Either "
                "point EMBEDDING_PROVIDER at a dedicated embeddings provider, or set "
                "EMBEDDING_DIMENSIONS explicitly to vouch for this model's output width - "
                "it is verified against the first real response before anything is indexed."
            )
        return EmbeddingModelSpec(
            provider="openrouter", model=model, dimensions=declared_dimensions
        )

    if declared_dimensions is None:
        known_for_provider = sorted(m for p, m in KNOWN_MODELS if p == provider)

        # A vendor-prefixed id is the router's naming convention, and a
        # frequent copy-paste from the chat settings. The direct API would
        # 404 on it, so say what to write instead of only listing models.
        vendor, _, bare = model.partition("/")
        if bare and (provider, bare) in KNOWN_MODELS:
            raise EmbeddingConfigurationError(
                f"EMBEDDING_MODEL={model!r} uses the vendor-prefixed form that routers such "
                f"as OpenRouter expect, but EMBEDDING_PROVIDER={provider} calls {vendor}'s API "
                f"directly, where the model is named {bare!r}. Either set "
                f"EMBEDDING_MODEL={bare} or set EMBEDDING_PROVIDER=openrouter together with "
                "EMBEDDING_DIMENSIONS."
            )

        suggestion = (
            f" Known {provider} models: {', '.join(known_for_provider)}."
            if known_for_provider
            else f" No embedding models are registered for provider {provider!r}."
        )
        raise EmbeddingConfigurationError(
            f"Unknown embedding model {provider}/{model} and no EMBEDDING_DIMENSIONS set, so "
            f"the vector width cannot be determined.{suggestion}"
        )

    return EmbeddingModelSpec(provider=provider, model=model, dimensions=declared_dimensions)
