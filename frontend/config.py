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

import os
from dataclasses import dataclass

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
    """Backend-only variables that are visible to this process.

    Reported in the UI rather than raising: refusing to start would be
    theatre, since the variable being *set* is not the leak - shipping it to
    a browser would be, and this app never does. What it is worth doing is
    telling whoever deployed it that their frontend process can read the
    database password, because that is one container compromise away from
    mattering.
    """
    return [name for name in FORBIDDEN_ENV_VARS if os.getenv(name)]
