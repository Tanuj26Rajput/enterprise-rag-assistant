"""Shared test fixtures.

A placeholder DSN is installed before ``app`` is imported for the first time:
settings validation requires ``DATABASE_URL``, and the suite never opens a
connection. Environment variables take precedence over ``.env``, so this also
keeps the suite independent of the developer's local file.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-abc")
# Pinned for the same reason as the DSN above. Settings validate the embedding
# and chat configuration at import time, so a developer's .env - which may
# legitimately name a model this process cannot resolve - would otherwise stop
# the suite from collecting at all.
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("EMBEDDING_MODEL", "hash-256")
os.environ.setdefault("LLM_PROVIDER", "openrouter")
os.environ.setdefault("LLM_MODEL", "anthropic/claude-sonnet-5.5")

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import deps
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.embeddings.hashing import HashingEmbeddingProvider
from app.main import create_app
from app.vectorstore.chroma import ChromaVectorStore
from tests.fakes import (
    FakeAuditRepository,
    FakeChatProvider,
    FakeCollectionRepository,
    FakeConversationRepository,
    FakeDocumentChunkRepository,
    FakeDocumentRepository,
    FakeJobRepository,
    FakeMessageRepository,
    FakeNamespaceMemberRepository,
    FakeNamespaceRepository,
    FakeSession,
    FakeStorage,
    FakeStore,
    FakeUserRepository,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        DATABASE_URL="postgresql://test:test@localhost:5432/test",
        ENVIRONMENT="test",
        SECRET_KEY="test-secret-key-that-is-long-enough-abc",
        # Small limits keep the upload-limit test from having to build a
        # 25 MB payload.
        MAX_UPLOAD_SIZE_MB=1,
        CHUNK_SIZE_TOKENS=64,
        CHUNK_OVERLAP_TOKENS=16,
        # Deterministic, offline embeddings. The suite must never reach a
        # paid API: it would be slow, flaky, and would bill whoever ran it.
        EMBEDDING_PROVIDER="hash",
        EMBEDDING_MODEL="hash-256",
        CHROMA_PERSIST_DIR=str(tmp_path / "chroma"),
    )


@pytest.fixture
def embeddings(settings: Settings) -> HashingEmbeddingProvider:
    return HashingEmbeddingProvider(settings.embedding_spec)


@pytest.fixture
def vector_store(settings: Settings) -> ChromaVectorStore:
    """A real Chroma store on a per-test directory.

    Not a fake. The vector store is where tenant filtering, idempotent
    upserts and deletion cleanup actually happen, so a double would be
    asserting against my own understanding of Chroma rather than against
    Chroma. It is cheap enough to use everywhere.
    """
    return ChromaVectorStore(
        persist_dir=settings.chroma_persist_dir, distance=settings.chroma_distance
    )


@pytest.fixture
def llm() -> FakeChatProvider:
    """The only scripted double outside the SQL layer.

    A real model would make the suite slow, costly and non-deterministic, and
    would test the vendor rather than this application. What these tests care
    about - what reaches the prompt, how citations are resolved, what each
    failure maps to - is entirely deterministic around it.
    """
    return FakeChatProvider()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
async def client(
    settings: Settings,
    store: FakeStore,
    storage: FakeStorage,
    session: FakeSession,
    vector_store: ChromaVectorStore,
    embeddings: HashingEmbeddingProvider,
    llm: FakeChatProvider,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the real app with the SQL layer replaced."""
    app = create_app(settings)

    async def _session() -> AsyncIterator[FakeSession]:
        yield session

    overrides: dict[Any, Any] = {
        get_settings: lambda: settings,
        get_db: _session,
        deps.get_user_repository: lambda: FakeUserRepository(store),
        deps.get_audit_repository: lambda: FakeAuditRepository(store),
        deps.get_namespace_repository: lambda: FakeNamespaceRepository(store),
        deps.get_member_repository: lambda: FakeNamespaceMemberRepository(store),
        deps.get_collection_repository: lambda: FakeCollectionRepository(store),
        deps.get_document_repository: lambda: FakeDocumentRepository(store),
        deps.get_chunk_repository: lambda: FakeDocumentChunkRepository(store),
        deps.get_job_repository: lambda: FakeJobRepository(store),
        deps.get_conversation_repository: lambda: FakeConversationRepository(store),
        deps.get_message_repository: lambda: FakeMessageRepository(store),
        deps.get_storage: lambda: storage,
        # Real Chroma, per-test directory; real (deterministic) embeddings.
        deps.get_vector_store: lambda: vector_store,
        deps.get_embeddings: lambda: embeddings,
        deps.get_llm: lambda: llm,
    }
    app.dependency_overrides.update(overrides)

    _apps.append(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


#: The application the active `client` fixture is bound to, so a test can
#: drive ASGI directly (see the disconnect tests) against the same overrides.
_apps: list[Any] = []


@pytest.fixture
async def app(client: AsyncClient) -> Any:
    return _apps[-1]


# ------------------------------------------------------------------ helpers ---
DEFAULT_PASSWORD = "correct-horse-battery-staple"


async def register(
    client: AsyncClient, email: str, password: str = DEFAULT_PASSWORD, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(
    client: AsyncClient, email: str, password: str = DEFAULT_PASSWORD
) -> dict[str, Any]:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def auth_header(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def signed_in(client: AsyncClient, email: str) -> dict[str, str]:
    """Register a user and return their Authorization header."""
    await register(client, email)
    return auth_header(await login(client, email))


async def make_namespace(
    client: AsyncClient, headers: dict[str, str], slug: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/namespaces",
        headers=headers,
        json={"slug": slug, "name": extra.pop("name", slug.title()), **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_collection(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    slug: str,
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/namespaces/{namespace_id}/collections",
        headers=headers,
        json={"slug": slug, "name": extra.pop("name", slug.title()), **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def add_member(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    email: str,
    role: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/namespaces/{namespace_id}/members",
        headers=headers,
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()
