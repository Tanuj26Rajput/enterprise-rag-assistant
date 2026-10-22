"""Chat generation: the provider interface, its implementations and prompts."""

from __future__ import annotations

from app.llm.base import (
    ChatChunk,
    ChatCompletion,
    ChatMessage,
    ChatProvider,
    ChatRequest,
    LLMConfigurationError,
    LLMError,
    LLMUnavailable,
)
from app.llm.factory import build_chat_provider, get_chat_provider
from app.llm.openai_compatible import OpenAICompatibleChatProvider

__all__ = [
    "ChatChunk",
    "ChatCompletion",
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "LLMConfigurationError",
    "LLMError",
    "LLMUnavailable",
    "OpenAICompatibleChatProvider",
    "build_chat_provider",
    "get_chat_provider",
]
