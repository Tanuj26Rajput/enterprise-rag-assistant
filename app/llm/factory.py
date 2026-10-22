"""Builds the configured chat provider."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.llm.base import (
    ChatChunk,
    ChatCompletion,
    ChatProvider,
    ChatRequest,
    LLMConfigurationError,
)
from app.llm.openai_compatible import OpenAICompatibleChatProvider

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Base URLs that need no explicit configuration.
_DEFAULT_BASE_URLS = {"openrouter": OPENROUTER_BASE_URL, "openai": None}

#: The key each provider falls back to when LLM_API_KEY is unset. Named here
#: so the error message can tell the operator which variable to set.
_KEY_NAMES = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"}


def build_chat_provider(settings: Settings | None = None) -> ChatProvider:
    """Construct the provider named by configuration.

    Raises :class:`LLMConfigurationError` rather than returning a provider
    that will fail on its first call.
    """
    settings = settings or get_settings()
    provider = settings.llm_provider

    api_key = settings.llm_api_key_value
    if not api_key:
        raise LLMConfigurationError(
            f"LLM_PROVIDER={provider} needs an API key: set LLM_API_KEY or {_KEY_NAMES[provider]}."
        )

    base_url = settings.llm_base_url or _DEFAULT_BASE_URLS[provider]

    if provider == "openrouter" and "/" not in settings.llm_model:
        # OpenRouter namespaces every model by vendor. A bare "gpt-4o-mini"
        # is a 404 at call time; saying so at startup is cheaper than
        # discovering it on a user's first question.
        raise LLMConfigurationError(
            f"OpenRouter model ids are namespaced by vendor, but LLM_MODEL is "
            f"{settings.llm_model!r}. Use e.g. 'openai/{settings.llm_model}'."
        )

    logger.info("Chat provider: %s/%s", provider, settings.llm_model)
    return OpenAICompatibleChatProvider(
        settings.llm_model,
        api_key=api_key,
        base_url=base_url,
        timeout=float(settings.llm_request_timeout),
        max_attempts=settings.llm_max_attempts,
        default_headers=settings.llm_default_headers,
        provider_label=provider,
    )


@lru_cache(maxsize=1)
def get_chat_provider() -> ChatProvider:
    """Process-wide provider, so the HTTP client and its pool are reused."""
    return build_chat_provider()


class LazyChatProvider:
    """A :class:`ChatProvider` that is not built until it is actually called.

    FastAPI resolves the whole dependency tree before an endpoint body runs,
    so a provider constructed in a dependency is constructed for *every*
    endpoint that transitively asks for one. ``ChatService`` is shared by
    listing, renaming and deleting conversations as well as by answering, and
    none of those three need a model. Building eagerly meant a deployment with
    no LLM key returned 500 from the entire conversation surface, including
    endpoints that would never have made a call.

    Deferring construction to first use keeps that surface working, and moves
    :class:`LLMConfigurationError` to where the caller is genuinely asking for
    generation - where it is mapped to 503 rather than escaping dependency
    resolution as an unhandled error.
    """

    __slots__ = ("_provider",)

    def __init__(self) -> None:
        self._provider: ChatProvider | None = None

    def _resolve(self) -> ChatProvider:
        if self._provider is None:
            self._provider = get_chat_provider()
        return self._provider

    @property
    def model(self) -> str:
        return self._resolve().model

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        return await self._resolve().complete(request)

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        # Deliberately not `async def`: `stream` must return the iterator
        # without awaiting, so the caller can close it on disconnect. Awaiting
        # here would break cancellation for every streamed answer.
        return self._resolve().stream(request)


__all__ = [
    "OPENROUTER_BASE_URL",
    "LazyChatProvider",
    "build_chat_provider",
    "get_chat_provider",
]
