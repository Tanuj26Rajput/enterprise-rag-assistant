"""The embedding registry, the providers, and the retry policy.

The registry tests are the ones that matter most here: every other guarantee
in the vector pipeline rests on the vector width being known and correct
before a collection is created.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.retry import retry_async
from app.embeddings.base import EmbeddingDimensionMismatch, verify_dimensions
from app.embeddings.hashing import HashingEmbeddingProvider
from app.embeddings.registry import (
    EmbeddingConfigurationError,
    EmbeddingModelSpec,
    resolve_spec,
)


def base_settings(**overrides: object) -> Settings:
    """Settings built from explicit values only.

    ``_env_file=None`` is load-bearing, not tidiness. Without it
    pydantic-settings falls back to the developer's real ``.env`` for every
    field not passed here, so a test asserting "this key is absent" passes or
    fails depending on whose machine it runs on - and a failure prints the
    real credential into the diff. These tests are about what the defaults
    and validators do, so they are given no ambient configuration at all.
    """
    defaults: dict[str, object] = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-abc",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_MODEL": "text-embedding-3-small",
    }
    return Settings(_env_file=None, **{**defaults, **overrides})  # type: ignore[arg-type]


class TestRegistry:
    def test_a_known_model_resolves_to_its_own_dimension(self) -> None:
        spec = resolve_spec("openai", "text-embedding-3-small")

        assert spec.dimensions == 1536
        assert spec.provider == "openai"

    def test_a_v3_model_may_be_shortened(self) -> None:
        spec = resolve_spec("openai", "text-embedding-3-large", declared_dimensions=1024)

        assert spec.dimensions == 1024
        assert spec.supports_dimension_override

    def test_a_model_cannot_be_lengthened_beyond_its_maximum(self) -> None:
        with pytest.raises(EmbeddingConfigurationError, match="at most"):
            resolve_spec("openai", "text-embedding-3-small", declared_dimensions=3072)

    def test_a_model_without_override_support_rejects_a_contradiction(self) -> None:
        """ada-002 ignores a dimensions argument rather than failing."""
        with pytest.raises(EmbeddingConfigurationError, match="contradicts"):
            resolve_spec("openai", "text-embedding-ada-002", declared_dimensions=512)

    def test_an_unknown_model_without_a_declared_dimension_is_refused(self) -> None:
        with pytest.raises(EmbeddingConfigurationError, match="cannot be determined"):
            resolve_spec("openai", "text-embedding-9-imaginary")


class TestOpenRouterIsNotAssumedCompatible:
    """OpenRouter fronts chat models; most serve no embeddings at all."""

    def test_an_unregistered_openrouter_model_is_refused(self) -> None:
        with pytest.raises(EmbeddingConfigurationError) as excinfo:
            resolve_spec("openrouter", "anthropic/claude-3.5-sonnet")

        message = str(excinfo.value)
        assert "no registered embedding dimension" in message
        # The error has to tell the operator what to do about it.
        assert "EMBEDDING_DIMENSIONS" in message

    def test_a_chat_model_name_is_not_borrowed_from_the_llm_settings(self) -> None:
        """Routing chat via OpenRouter must not route embeddings there."""
        settings = base_settings(LLM_MODEL="anthropic/claude-3.5-sonnet")

        assert settings.embedding_provider == "openai"
        assert settings.embedding_spec.model == "text-embedding-3-small"

    def test_an_explicitly_vouched_openrouter_model_is_allowed(self) -> None:
        spec = resolve_spec("openrouter", "some/embedding-model", declared_dimensions=768)

        assert spec.dimensions == 768
        assert spec.provider == "openrouter"

    def test_settings_refuse_openrouter_without_a_declared_dimension(self) -> None:
        with pytest.raises(EmbeddingConfigurationError, match="no registered embedding dimension"):
            base_settings(EMBEDDING_PROVIDER="openrouter", EMBEDDING_MODEL="meta-llama/llama-3-8b")

    def test_an_openai_key_is_not_handed_to_openrouter(self) -> None:
        """A credential must not leak to a third party by adjacency."""
        settings = base_settings(
            EMBEDDING_PROVIDER="openrouter",
            EMBEDDING_MODEL="some/embedding-model",
            EMBEDDING_DIMENSIONS=768,
            OPENAI_API_KEY="sk-not-for-openrouter",
        )

        assert settings.embedding_api_key_value is None

    def test_the_openrouter_key_is_reused_for_the_openrouter_provider(self) -> None:
        """Falling back within one vendor is safe, and is what an operator expects.

        The rule is "never across vendors", not "never": OPENROUTER_API_KEY
        with EMBEDDING_PROVIDER=openrouter is unambiguous, and requiring a
        duplicate EMBEDDING_API_KEY for it would be ceremony, not safety.
        """
        settings = base_settings(
            EMBEDDING_PROVIDER="openrouter",
            EMBEDDING_MODEL="nvidia/nemotron-3-embed-1b:free",
            EMBEDDING_DIMENSIONS=2048,
            OPENROUTER_API_KEY="sk-or-example",
        )

        assert settings.embedding_api_key_value == "sk-or-example"

    def test_an_explicit_embedding_key_still_wins(self) -> None:
        settings = base_settings(
            EMBEDDING_PROVIDER="openrouter",
            EMBEDDING_MODEL="some/embedding-model",
            EMBEDDING_DIMENSIONS=768,
            OPENROUTER_API_KEY="sk-or-fallback",
            EMBEDDING_API_KEY="sk-explicit",
        )

        assert settings.embedding_api_key_value == "sk-explicit"

    def test_the_openai_key_is_reused_for_the_openai_provider(self) -> None:
        settings = base_settings(EMBEDDING_PROVIDER="openai", OPENAI_API_KEY="sk-example-key")

        assert settings.embedding_api_key_value == "sk-example-key"


class TestSettingsValidation:
    def test_an_unresolvable_model_fails_at_startup_not_at_index_time(self) -> None:
        with pytest.raises(EmbeddingConfigurationError, match="cannot be determined"):
            base_settings(EMBEDDING_MODEL="text-embedding-9-imaginary")

    def test_hash_embeddings_are_refused_in_production(self) -> None:
        with pytest.raises(ValueError, match="must not be used in production"):
            base_settings(
                ENVIRONMENT="production",
                EMBEDDING_PROVIDER="hash",
                EMBEDDING_MODEL="hash-256",
                SECRET_KEY="a-long-enough-production-secret-key-value",
                CORS_ORIGINS="https://app.example.com",
            )

    def test_a_blank_env_entry_is_treated_as_unset(self) -> None:
        """`KEY=` in a .env file means "not configured", not an empty value."""
        settings = base_settings(EMBEDDING_PROVIDER="", CHROMA_HOST="")

        assert settings.embedding_provider == "openai"
        assert settings.chroma_host is None


class TestDimensionVerification:
    def test_a_wrong_width_response_is_rejected(self) -> None:
        spec = EmbeddingModelSpec(provider="openai", model="m", dimensions=4)

        with pytest.raises(EmbeddingDimensionMismatch, match="3-dimensional"):
            verify_dimensions([[0.0] * 4, [0.0] * 3], spec)

    def test_correct_widths_pass_through(self) -> None:
        spec = EmbeddingModelSpec(provider="openai", model="m", dimensions=2)
        vectors = [[0.1, 0.2], [0.3, 0.4]]

        assert verify_dimensions(vectors, spec) == vectors


class TestHashingProvider:
    @pytest.fixture
    def provider(self) -> HashingEmbeddingProvider:
        return HashingEmbeddingProvider(resolve_spec("hash", "hash-256"))

    async def test_vectors_have_the_declared_width(
        self, provider: HashingEmbeddingProvider
    ) -> None:
        vectors = await provider.embed_documents(["alpha beta", "gamma"])

        assert [len(v) for v in vectors] == [256, 256]

    async def test_embedding_is_deterministic(self, provider: HashingEmbeddingProvider) -> None:
        """Re-indexing unchanged text must not produce different vectors."""
        first = await provider.embed_query("the retention policy")
        second = await provider.embed_query("the retention policy")

        assert first == second

    async def test_identical_text_outranks_unrelated_text(
        self, provider: HashingEmbeddingProvider
    ) -> None:
        query = await provider.embed_query("annual leave entitlement")
        same, other = await provider.embed_documents(
            ["annual leave entitlement", "fire extinguisher inspection schedule"]
        )

        def dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b, strict=True))

        assert dot(query, same) > dot(query, other)

    async def test_empty_text_yields_a_valid_vector(
        self, provider: HashingEmbeddingProvider
    ) -> None:
        """A zero-norm vector must not divide by zero."""
        vector = await provider.embed_query("")

        assert len(vector) == 256
        assert set(vector) == {0.0}


class TestRetry:
    async def test_it_retries_until_it_succeeds(self) -> None:
        attempts = 0

        async def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TimeoutError("transient")
            return "ok"

        result = await retry_async(flaky, attempts=5, base_delay=0, retry_on=(TimeoutError,))

        assert result == "ok"
        assert attempts == 3

    async def test_it_gives_up_and_reraises(self) -> None:
        attempts = 0

        async def always_fails() -> str:
            nonlocal attempts
            attempts += 1
            raise TimeoutError("still down")

        with pytest.raises(TimeoutError):
            await retry_async(always_fails, attempts=3, base_delay=0, retry_on=(TimeoutError,))

        assert attempts == 3

    async def test_an_unlisted_error_is_not_retried(self) -> None:
        """Retrying a bad request re-sends a bad request."""
        attempts = 0

        async def bad_request() -> str:
            nonlocal attempts
            attempts += 1
            raise ValueError("malformed")

        with pytest.raises(ValueError, match="malformed"):
            await retry_async(bad_request, attempts=5, base_delay=0, retry_on=(TimeoutError,))

        assert attempts == 1
