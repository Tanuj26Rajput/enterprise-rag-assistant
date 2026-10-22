"""Chat completions over an OpenAI-compatible HTTP API.

One class serves OpenRouter and OpenAI, because OpenRouter implements the
OpenAI wire format exactly; what differs is the base URL, the model naming
convention (``vendor/model``) and two optional attribution headers.

The same retry classification as the embedding provider applies, for the same
reason: re-sending a rejected request produces another rejection, more slowly
and at the same cost.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from app.core.retry import retry_async
from app.llm.base import (
    ChatChunk,
    ChatCompletion,
    ChatRequest,
    LLMConfigurationError,
    LLMError,
    LLMUnavailable,
)

logger = logging.getLogger(__name__)

#: Failures a later attempt could plausibly survive.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
)


class OpenAICompatibleChatProvider:
    """Calls an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_attempts: int = 3,
        default_headers: dict[str, str] | None = None,
        provider_label: str = "llm",
    ) -> None:
        self._model = model
        self._max_attempts = max_attempts
        self._label = provider_label
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            # The SDK retries some statuses itself. Disabled so there is one
            # retry policy with one backoff curve, not two multiplying.
            max_retries=0,
            default_headers=default_headers or None,
        )

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            **request.extra,
        }

        async def call() -> Any:
            return await self._client.chat.completions.create(**payload)

        try:
            response = await retry_async(
                call,
                attempts=self._max_attempts,
                retry_on=TRANSIENT_ERRORS,
                description=f"completion via {self._label}/{self._model}",
            )
        except NotFoundError as exc:
            # On a router this is the usual way "that model does not exist,
            # or you have no access to it" arrives.
            raise LLMConfigurationError(
                f"{self._label} has no model {self._model!r} available to this account. "
                "Check LLM_MODEL against the provider's model list."
            ) from exc
        except AuthenticationError as exc:
            raise LLMConfigurationError(f"The {self._label} credentials were rejected.") from exc
        except PermissionDeniedError as exc:
            raise LLMConfigurationError(
                f"{self._label} refused access to {self._model!r}."
            ) from exc
        except BadRequestError as exc:
            raise LLMConfigurationError(
                f"{self._label} rejected the request for {self._model!r}: {exc}"
            ) from exc
        except TRANSIENT_ERRORS as exc:
            raise LLMUnavailable(
                f"{self._label} was unreachable after {self._max_attempts} attempt(s)."
            ) from exc

        return self._to_completion(response)

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Yield deltas as they arrive, then one final chunk.

        No retry wraps the stream. Once the first token has been handed to the
        client, a retry would restart the answer mid-sentence; a failure part
        way through is reported to the caller instead, which can end the SSE
        stream with an error event rather than silently producing two answers
        spliced together. Failures *before* the first token still surface as
        the same exception types as :meth:`complete`.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
            # Ask for a usage summary on the final SSE frame. OpenRouter and
            # OpenAI both honour this; providers that ignore it simply leave
            # the token counts None, which the caller already tolerates.
            "stream_options": {"include_usage": True},
            **request.extra,
        }

        model = self._model
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        try:
            stream = await self._client.chat.completions.create(**payload)
            async for event in stream:
                model = getattr(event, "model", None) or model
                if usage := getattr(event, "usage", None):
                    prompt_tokens = getattr(usage, "prompt_tokens", None)
                    completion_tokens = getattr(usage, "completion_tokens", None)

                choices = getattr(event, "choices", None) or []
                if not choices:
                    # The usage-only frame at the end of the stream.
                    continue
                choice = choices[0]
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = getattr(getattr(choice, "delta", None), "content", None)
                if delta:
                    yield ChatChunk(delta=delta)
        except NotFoundError as exc:
            raise LLMConfigurationError(
                f"{self._label} has no model {self._model!r} available to this account."
            ) from exc
        except AuthenticationError as exc:
            raise LLMConfigurationError(f"The {self._label} credentials were rejected.") from exc
        except (PermissionDeniedError, BadRequestError) as exc:
            raise LLMConfigurationError(
                f"{self._label} rejected the streaming request for {self._model!r}."
            ) from exc
        except TRANSIENT_ERRORS as exc:
            raise LLMUnavailable(f"{self._label} failed mid-stream: {exc}") from exc

        yield ChatChunk(
            done=True,
            model=model,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _to_completion(self, response: Any) -> ChatCompletion:
        choices = getattr(response, "choices", None)
        if not choices:
            # A router returning a well-formed envelope with nothing in it is
            # rare but real, usually when an upstream provider errors after
            # the response has started.
            raise LLMError(f"{self._label} returned no completion choices.")

        choice = choices[0]
        content = getattr(choice.message, "content", None) or ""
        usage = getattr(response, "usage", None)

        return ChatCompletion(
            content=content,
            # What the router actually served, which can differ from what was
            # asked for; worth reporting rather than echoing the request.
            model=getattr(response, "model", None) or self._model,
            finish_reason=getattr(choice, "finish_reason", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        )
