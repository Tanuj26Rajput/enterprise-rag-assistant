"""Everything the frontend is allowed to know.

Exactly one setting, and it is a URL to our own API. That is the whole point
of this module: the browser-facing process holds no database credentials, no
OpenAI or OpenRouter key, and no signing secret. It cannot leak what it was
never given, and an operator reading this file can confirm that in ten
seconds rather than auditing the whole app.

Everything the UI shows comes back over HTTP from the backend, which is where
the keys live and where every authorization decision is made. If this process
were compromised outright, the attacker would hold whatever tokens the
currently signed-in users have - and nothing else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Any of these in the environment would mean a secret has leaked into the
#: frontend's process. Checked at start-up rather than trusted, because the
#: usual way this goes wrong is a single shared .env file.
FORBIDDEN_ENV_VARS = (
    "DATABASE_URL",
    "DIRECT_DATABASE_URL",
    "SECRET_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_API_KEY",
    "EMBEDDING_API_KEY",
)

DEFAULT_API_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True, slots=True)
class FrontendSettings:
    api_url: str
    request_timeout: float = 30.0
    #: Generous: a long answer can take a while to finish streaming, and the
    #: backend sends keep-alive comments so a quiet stream is not a dead one.
    stream_timeout: float = 300.0
    upload_timeout: float = 300.0

    @property
    def api_root(self) -> str:
        return f"{self.api_url.rstrip('/')}/api/v1"


def load_settings() -> FrontendSettings:
    return FrontendSettings(
        api_url=os.getenv("RAG_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL,
        request_timeout=float(os.getenv("RAG_API_TIMEOUT", "30")),
    )


def leaked_secrets() -> list[str]:
    """Backend-only variables that are visible to this process."""
    return [name for name in FORBIDDEN_ENV_VARS if os.getenv(name)]


def warn_about_leaked_secrets() -> None:
    """Log, once, if this process can read backend-only variables.

    Logged rather than rendered. The condition is real - a shared `.env` file
    hands the browser-facing process the database password - but it is a
    deployment mistake, and the person who can fix it reads logs, not the
    chat UI. Every end user seeing a warning they cannot act on is noise that
    trains people to ignore warnings.

    Not raised either: refusing to start would be theatre, since the variable
    being *set* is not the leak. Shipping it to a browser would be, and this
    app never does.
    """
    leaked = leaked_secrets()
    if not leaked:
        return
    logger.warning(
        "This frontend process can read backend-only variables: %s. "
        "The UI never sends them anywhere, but it should not be able to see "
        "them either - give the frontend its own environment with RAG_API_URL "
        "alone (docker-compose.yml does this deliberately).",
        ", ".join(leaked),
    )
