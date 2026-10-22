"""Retry with exponential backoff for transient remote failures.

Written here rather than pulled in as a dependency because the interesting
part is not the backoff loop, it is deciding *what to retry*. Retrying a 401
re-sends a bad key; retrying a 400 re-sends a malformed request; both burn
quota and delay the error the operator needs to see. Only failures that a
later attempt could plausibly survive are retried.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """A failure the caller has classified as worth another attempt."""


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[Exception], ...] = (RetryableError,),
    description: str = "operation",
) -> T:
    """Call ``operation`` until it succeeds or the attempts run out.

    Backoff is exponential with full jitter. Without jitter, a batch of
    documents failing together retries in lockstep and reproduces the
    overload that caused the failure.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last = exc
            if attempt == attempts:
                break
            delay = random.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
            logger.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.2fs",
                description,
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    # The loop cannot exit without setting this.
    assert last is not None
    logger.error("%s failed after %d attempts: %s", description, attempts, last)
    raise last
