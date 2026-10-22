"""Builds the configured chat provider."""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.llm.base import ChatProvider, LLMConfigurationError
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


__all__ = [
    "OPENROUTER_BASE_URL",
    "build_chat_provider",
    "get_chat_provider",
]
