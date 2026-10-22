"""The chat provider interface.

Generation sits behind this protocol for the same reason retrieval sits behind
``VectorStore``: the answer service should be testable without a network, and
swapping OpenRouter for a direct vendor client should be a configuration
change rather than an edit to the service that builds prompts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """One model response, plus what it cost and why it stopped."""

    content: str
    model: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens

    @property
    def was_truncated(self) -> bool:
        """Whether the model ran out of budget mid-sentence.

        Worth surfacing rather than hiding: a truncated answer can end in the
        middle of a citation, and the caller may prefer to say so.
        """
        return self.finish_reason == "length"


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """What to ask, and the knobs the deployment is allowed to turn."""

    messages: list[ChatMessage]
    temperature: float = 0.1
    max_tokens: int = 1024
    extra: dict[str, object] = field(default_factory=dict)


class LLMError(RuntimeError):
    """A generation call failed in a way the caller must handle."""


class LLMConfigurationError(LLMError):
    """The deployment is misconfigured - a later attempt cannot help.

    Kept distinct from a transient failure because the two deserve different
    HTTP statuses and different operator attention: one needs a fix, the
    other needs patience.
    """


class LLMUnavailable(LLMError):
    """The provider could not be reached, or gave up, after retrying."""


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One increment of a streamed response.

    ``delta`` carries new text. The final chunk of a stream carries no text
    and sets ``done``, plus whatever usage and finish reason the provider
    reported, so a caller can persist the turn without a second request.
    """

    delta: str = ""
    done: bool = False
    model: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@runtime_checkable
class ChatProvider(Protocol):
    """Turns a conversation into a completion."""

    @property
    def model(self) -> str:
        """The model identifier this provider will call."""
        ...

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        """Run one non-streaming completion."""
        ...

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Run one streaming completion.

        Not an ``async def``: implementations are async generators, so the
        call itself returns the iterator without awaiting. That matters for
        cancellation - the caller owns the generator and can close it when a
        client disconnects, which is what stops the upstream request.
        """
        ...
