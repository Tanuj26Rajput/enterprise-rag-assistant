"""Application configuration.

All runtime configuration is loaded from environment variables (or a local
``.env`` file) and validated once by Pydantic. Nothing else in the codebase
reads ``os.environ`` directly: modules call :func:`get_settings` instead, so an
invalid deployment fails fast and loudly rather than halfway through a request.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    Field,
    PostgresDsn,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from app.embeddings.registry import EmbeddingModelSpec

Environment = Literal["development", "staging", "production", "test"]

# Supabase exposes two endpoints for the same database:
#   * port 5432 -> direct connection (session mode, supports prepared statements)
#   * port 6543 -> Supavisor / PgBouncer in *transaction* mode
# Transaction pooling multiplexes server connections between clients, so
# server-side prepared statements and client-side connection pooling both break.
# We detect that endpoint from the URL and adjust the engine accordingly.
_POOLER_PORT = 6543


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---------------------------------------------------------------- app ---
    app_name: str = "Enterprise RAG Assistant"
    app_version: str = "0.1.0"
    environment: Environment = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    # Bind address for the development entrypoint (run.py). Defaults to
    # loopback so a dev server is not exposed on the local network by
    # accident; a container sets HOST=0.0.0.0 deliberately.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = False

    # Kept as a raw string so the value parses identically whether it is given
    # as a JSON array or as a plain comma-separated list (see `cors_origins`).
    cors_origins_raw: str = Field(default="*", alias="CORS_ORIGINS")

    # ----------------------------------------------------------- security ---
    secret_key: SecretStr = SecretStr("change-me-in-production")
    jwt_algorithm: Literal["HS256", "HS512"] = "HS256"
    access_token_expire_minutes: int = Field(default=30, ge=1, le=24 * 60)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=90)
    password_min_length: int = Field(default=12, ge=8, le=128)

    # ----------------------------------------------------------- database ---
    # Runtime connection. Point this at the Supabase transaction pooler
    # (port 6543) for serverless or container deployments.
    database_url: PostgresDsn

    # Optional direct (non-pooled, port 5432) connection. DDL and session-level
    # advisory locks are unreliable through a transaction pooler, so Alembic
    # prefers this URL whenever it is configured.
    database_direct_url: PostgresDsn | None = None

    db_echo: bool = False
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout: int = Field(default=30, ge=1, le=300)
    db_pool_recycle: int = Field(default=1800, ge=60, le=14400)
    db_pool_pre_ping: bool = True
    db_connect_timeout: int = Field(default=10, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=600_000)

    # ---------------------------------------------------------------- llm ---
    # Answer generation. OpenRouter is the default because it speaks the
    # OpenAI wire format while leaving the model a deployment decision rather
    # than a code change - the point of routing in the first place.
    llm_provider: Literal["openrouter", "openai"] = "openrouter"
    llm_model: str = "anthropic/claude-sonnet-5.5"
    openai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None

    #: Low by default: this is extractive question answering over supplied
    #: passages, where creative variation is a defect, not a feature.
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1024, ge=64, le=32_000)
    llm_request_timeout: int = Field(default=60, ge=5, le=600)
    llm_max_attempts: int = Field(default=3, ge=1, le=10)

    #: OpenRouter attribution headers. Optional, and sent only to OpenRouter:
    #: they identify the calling app on its dashboards and leaderboards.
    openrouter_site_url: str | None = None
    openrouter_app_name: str | None = None

    #: How much of each retrieved chunk is pasted into the prompt. Chunks are
    #: already bounded by CHUNK_SIZE_TOKENS; this is a second ceiling so a
    #: large top_k cannot silently produce an enormous, expensive request.
    answer_excerpt_chars: int = Field(default=1_200, ge=200, le=8_000)

    # --------------------------------------------------------------- chat ---
    #: Prior messages replayed to the model. Bounded on two axes because
    #: either one alone can be defeated: a long thread of short turns blows
    #: the message bound's token budget, and a short thread of enormous turns
    #: blows the character bound's message count.
    chat_history_messages: int = Field(default=10, ge=0, le=100)
    chat_history_max_chars: int = Field(default=12_000, ge=500, le=200_000)
    #: Characters of the first question kept as a thread title.
    chat_title_length: int = Field(default=80, ge=16, le=255)
    # ----------------------------------------------------------- embedding ---
    # Deliberately independent of the chat settings above. Routing chat through
    # OpenRouter says nothing about where embeddings should go: OpenRouter
    # fronts chat models, and most of them have no embeddings endpoint at all.
    # See app/embeddings/registry.py.
    embedding_provider: Literal["openai", "openrouter", "hash"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: SecretStr | None = None
    embedding_base_url: str | None = None

    #: Left unset, the width comes from the registry. Set, it either shortens
    #: a model that supports shortening or vouches for a model the registry
    #: does not know - and is checked against the first real response.
    embedding_dimensions: int | None = Field(default=None, ge=64, le=4096)

    embedding_batch_size: int = Field(default=64, ge=1, le=512)
    embedding_max_attempts: int = Field(default=3, ge=1, le=10)
    embedding_request_timeout: int = Field(default=60, ge=5, le=600)

    # ------------------------------------------------------------ vectors ---
    chroma_persist_dir: str = "./var/chroma"
    chroma_host: str | None = None
    chroma_port: int = Field(default=8000, ge=1, le=65535)
    #: Cosine suits normalised text embeddings and is what every model in the
    #: registry is trained for. Recorded on the Chroma collection at creation
    #: and immutable thereafter.
    chroma_distance: Literal["cosine", "l2", "ip"] = "cosine"

    # ---------------------------------------------------------- retrieval ---
    chunk_size_tokens: int = Field(default=512, ge=64, le=4096)
    chunk_overlap_tokens: int = Field(default=64, ge=0, le=1024)
    retrieval_top_k: int = Field(default=5, ge=1, le=50)

    # ---------------------------------------------------------- ingestion ---
    #: ``inline`` runs a document's ingestion job inside the request that
    #: created it: one process to deploy, and the upload response reports the
    #: real outcome. ``background`` writes the job and returns immediately,
    #: leaving a worker to claim it - the mode a large corpus needs, where a
    #: 200-page PDF must not hold an HTTP connection open.
    #:
    #: Both modes write the same durable job row first, so the difference is
    #: *who* runs it, never whether a crash loses it.
    ingestion_mode: Literal["inline", "background"] = "inline"
    #: Attempts per job, including the first. Three covers a provider blip
    #: without turning a reproducible failure into a long, expensive loop.
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    #: How long a worker's claim on a job is believed. Must exceed the
    #: slowest realistic document: a lease that expires mid-job hands the
    #: same work to a second worker. Idempotent jobs make that survivable,
    #: not free.
    ingestion_lease_seconds: int = Field(default=600, ge=30, le=7200)
    #: Worker idle poll interval. Short enough to feel immediate, long enough
    #: that an idle deployment is not querying continuously.
    ingestion_poll_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    #: Vector ids examined per page when reconciling a collection.
    ingestion_reconcile_batch: int = Field(default=500, ge=10, le=10_000)

    # ------------------------------------------------------------ uploads ---
    max_upload_size_mb: int = Field(default=25, ge=1, le=500)
    upload_dir: str = "./var/uploads"
    allowed_upload_types_raw: str = Field(
        default=(
            "application/pdf,text/plain,text/markdown,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        alias="ALLOWED_UPLOAD_TYPES",
    )

    # --------------------------------------------------------- validators ---
    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: Any) -> Any:
        """Treat ``KEY=`` in a .env file as absent rather than as ``""``.

        A template .env lists every key with an empty value, and the keys a
        deployment does not use are left that way. Without this, every one of
        them fails validation against its own type - an empty string is not a
        valid port, URL or enum member - and the operator is told their
        configuration is invalid when they simply have not set it.
        """
        if isinstance(value, str) and not value.strip():
            raise PydanticUseDefault
        return value

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def _overlap_fits_in_chunk(cls, value: int, info: ValidationInfo) -> int:
        chunk_size = info.data.get("chunk_size_tokens")
        if chunk_size is not None and value >= chunk_size:
            raise ValueError("chunk_overlap_tokens must be smaller than chunk_size_tokens")
        return value

    @model_validator(mode="after")
    def _enforce_production_hardening(self) -> Settings:
        if self.environment != "production":
            return self
        secret = self.secret_key.get_secret_value()
        if secret in {"", "change-me-in-production"}:
            raise ValueError("SECRET_KEY must be set to a strong value in production")
        if len(secret) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters in production")
        if self.debug:
            raise ValueError("DEBUG must be disabled in production")
        if "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS must not be a wildcard in production")
        if self.embedding_provider == "hash":
            # Hash embeddings are syntactic, not semantic. A production
            # collection built on them would retrieve plausible-looking
            # nonsense and raise no error at any point.
            raise ValueError(
                "EMBEDDING_PROVIDER=hash is a development stand-in and must not be used "
                "in production"
            )
        return self

    @model_validator(mode="after")
    def _embedding_model_is_resolvable(self) -> Settings:
        """Fail at startup if the vector width cannot be established.

        Deferred to a validator so a bad combination is caught when settings
        load, not when the first document is indexed into a collection that
        has already been created at the wrong width.
        """
        self.embedding_spec  # noqa: B018 - raises if unresolvable
        return self

    # --------------------------------------------------------- accessors ---
    @property
    def cors_origins(self) -> list[str]:
        """CORS origins as a list (accepts ``a,b`` or a JSON array)."""
        raw = self.cors_origins_raw.strip()
        if raw.startswith("["):
            return [str(origin).strip() for origin in json.loads(raw)]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def allowed_upload_types(self) -> set[str]:
        return {item.strip() for item in self.allowed_upload_types_raw.split(",") if item.strip()}

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    # --------------------------------------------------------- embedding ---
    @property
    def embedding_spec(self) -> EmbeddingModelSpec:
        """The resolved model and vector width.

        Imported lazily: the registry imports nothing from this module, but
        keeping the import local documents that configuration depends on the
        registry and never the reverse.
        """
        from app.embeddings.registry import resolve_spec

        return resolve_spec(
            self.embedding_provider,
            self.embedding_model,
            declared_dimensions=self.embedding_dimensions,
        )

    @property
    def embedding_api_key_value(self) -> str | None:
        """The embedding key, falling back only within the same vendor.

        ``EMBEDDING_API_KEY`` wins. Otherwise each provider falls back to its
        own named key and no other: an OpenAI key is never sent to OpenRouter
        and vice versa, because two credentials sitting in the same ``.env``
        is not consent to hand one of them to the other vendor.

        Falling back *within* a provider is a different matter and is safe -
        ``EMBEDDING_PROVIDER=openrouter`` with ``OPENROUTER_API_KEY`` set is
        unambiguous, and it mirrors what ``llm_api_key_value`` already does.
        """
        if self.embedding_api_key is not None:
            return self.embedding_api_key.get_secret_value()
        if self.embedding_provider == "openai" and self.openai_api_key is not None:
            return self.openai_api_key.get_secret_value()
        if self.embedding_provider == "openrouter" and self.openrouter_api_key is not None:
            return self.openrouter_api_key.get_secret_value()
        return None

    # --------------------------------------------------------------- llm ---
    @property
    def llm_api_key_value(self) -> str | None:
        """The chat key, resolved without crossing providers.

        ``LLM_API_KEY`` wins if set. Otherwise each provider falls back only
        to its own named key: an OpenAI key is never sent to OpenRouter and
        vice versa. Two credentials sitting in the same ``.env`` is not
        consent to send one of them to the other vendor.
        """
        if self.llm_api_key is not None:
            return self.llm_api_key.get_secret_value()
        if self.llm_provider == "openrouter" and self.openrouter_api_key is not None:
            return self.openrouter_api_key.get_secret_value()
        if self.llm_provider == "openai" and self.openai_api_key is not None:
            return self.openai_api_key.get_secret_value()
        return None

    @property
    def llm_default_headers(self) -> dict[str, str]:
        """Attribution headers, sent to OpenRouter only."""
        if self.llm_provider != "openrouter":
            return {}
        headers = {}
        if self.openrouter_site_url:
            headers["HTTP-Referer"] = self.openrouter_site_url
        if self.openrouter_app_name:
            headers["X-Title"] = self.openrouter_app_name
        return headers

    # ---------------------------------------------------------- database ---
    @staticmethod
    def _as_async_url(dsn: PostgresDsn | str) -> str:
        """Force the SQLAlchemy ``postgresql+psycopg`` (async-capable) driver."""
        _, _, remainder = str(dsn).partition("://")
        return f"postgresql+psycopg://{remainder}"

    @property
    def async_database_url(self) -> str:
        """URL used by the application engine."""
        return self._as_async_url(self.database_url)

    @property
    def migration_database_url(self) -> str:
        """URL used by Alembic: the direct endpoint when one is configured."""
        return self._as_async_url(self.database_direct_url or self.database_url)

    @property
    def uses_transaction_pooler(self) -> bool:
        """True when the runtime URL points at a *transaction*-mode pooler.

        The mode is decided by the port, not by the hostname. Supabase serves
        both modes from the same ``*.pooler.supabase.com`` host: 6543 is
        transaction mode, where a connection is handed to another client
        between transactions, and 5432 is session mode, which holds a backend
        for the life of the connection. Treating session mode as transaction
        mode is safe but wasteful - it would discard client-side pooling and
        server-side prepared statements that session mode supports.

        6543 is also the conventional PgBouncer transaction-mode port, so this
        holds for a self-hosted pooler too.
        """
        return urlsplit(str(self.database_url)).port == _POOLER_PORT

    @property
    def db_connect_args(self) -> dict[str, Any]:
        """``connect_args`` passed to psycopg3.

        ``options`` is applied by PostgreSQL to every new backend, which is the
        only reliable way to pin a statement timeout when connections may be
        recycled underneath us by a pooler.
        """
        args: dict[str, Any] = {
            "connect_timeout": self.db_connect_timeout,
            "application_name": f"{self.app_name} ({self.environment})",
            "options": f"-c statement_timeout={self.db_statement_timeout_ms}",
        }
        if self.uses_transaction_pooler:
            # A server-side prepared statement cannot survive its connection
            # being handed to another client between transactions.
            args["prepare_threshold"] = None
        return args


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so validation runs once and so FastAPI can depend on it directly.
    Call ``get_settings.cache_clear()`` in tests that patch the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values are supplied by the environment
