"""FastAPI dependencies.

Dependencies are the single place where a request is turned into an
authenticated, authorized context. A handler that does not declare
:data:`CurrentUser` is unauthenticated by construction, which makes the
security posture of each route readable from its signature.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.db.models import NamespaceRole, User
from app.db.session import get_db
from app.embeddings.base import EmbeddingProvider
from app.embeddings.factory import get_embedding_provider
from app.llm.base import ChatProvider
from app.llm.factory import get_chat_provider
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.repositories.conversation import ConversationRepository, MessageRepository
from app.repositories.document import DocumentChunkRepository, DocumentRepository
from app.repositories.job import JobRepository
from app.repositories.namespace import NamespaceMemberRepository, NamespaceRepository
from app.repositories.user import UserRepository
from app.schemas.common import PageParams
from app.services.auth import AuthService, RequestContext
from app.services.authorization import NamespaceAuthorizer, NamespaceContext
from app.services.chat import ChatService
from app.services.collection import CollectionService
from app.services.document import DocumentService
from app.services.generation import AnswerService
from app.services.indexing import IndexingService
from app.services.lifecycle import DocumentLifecycleService
from app.services.namespace import NamespaceService
from app.services.retrieval import RetrievalService
from app.services.storage import BlobStorage, LocalFileStorage
from app.vectorstore.base import VectorStore
from app.vectorstore.chroma import ChromaVectorStore

# auto_error=False so a missing header raises our own AuthenticationError and
# produces the same error envelope as every other failure.
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]
PageParamsDep = Annotated[PageParams, Depends()]


def get_request_context(request: Request) -> RequestContext:
    """Caller metadata recorded on every audit entry."""
    return RequestContext(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


RequestContextDep = Annotated[RequestContext, Depends(get_request_context)]


# ------------------------------------------------------------- repositories ---
def get_user_repository(session: SessionDep) -> UserRepository:
    return UserRepository(session)


def get_audit_repository(session: SessionDep) -> AuditRepository:
    return AuditRepository(session)


def get_namespace_repository(session: SessionDep) -> NamespaceRepository:
    return NamespaceRepository(session)


def get_member_repository(session: SessionDep) -> NamespaceMemberRepository:
    return NamespaceMemberRepository(session)


def get_collection_repository(session: SessionDep) -> CollectionRepository:
    return CollectionRepository(session)


def get_document_repository(session: SessionDep) -> DocumentRepository:
    return DocumentRepository(session)


def get_chunk_repository(session: SessionDep) -> DocumentChunkRepository:
    return DocumentChunkRepository(session)


def get_storage(settings: SettingsDep) -> BlobStorage:
    return LocalFileStorage(settings.upload_dir)


# -------------------------------------------------------------- vector stack ---
@lru_cache(maxsize=1)
def _shared_vector_store() -> VectorStore:
    """One store per process.

    Chroma's embedded client owns its files, and building a second one per
    request would reopen the index on every call.
    """
    settings = get_settings()
    return ChromaVectorStore(
        persist_dir=settings.chroma_persist_dir,
        host=settings.chroma_host,
        port=settings.chroma_port,
        distance=settings.chroma_distance,
    )


def get_vector_store() -> VectorStore:
    return _shared_vector_store()


#: Exported so the readiness probe can reach the same store the request path
#: uses - including whatever a test has overridden it with. Probing a store
#: the application does not actually use would be a probe of nothing.
VectorStoreDep = Annotated[VectorStore, Depends(get_vector_store)]


def get_embeddings() -> EmbeddingProvider:
    return get_embedding_provider()


def get_indexing_service(
    store: Annotated[VectorStore, Depends(get_vector_store)],
    embeddings: Annotated[EmbeddingProvider, Depends(get_embeddings)],
    settings: SettingsDep,
) -> IndexingService:
    return IndexingService(store, embeddings, settings)


IndexingServiceDep = Annotated[IndexingService, Depends(get_indexing_service)]


# ----------------------------------------------------------------- services ---
def get_authorizer(
    namespaces: Annotated[NamespaceRepository, Depends(get_namespace_repository)],
    members: Annotated[NamespaceMemberRepository, Depends(get_member_repository)],
) -> NamespaceAuthorizer:
    return NamespaceAuthorizer(namespaces, members)


AuthorizerDep = Annotated[NamespaceAuthorizer, Depends(get_authorizer)]


def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    settings: SettingsDep,
) -> AuthService:
    return AuthService(users, audit, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_namespace_service(
    namespaces: Annotated[NamespaceRepository, Depends(get_namespace_repository)],
    members: Annotated[NamespaceMemberRepository, Depends(get_member_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    authorizer: AuthorizerDep,
) -> NamespaceService:
    return NamespaceService(namespaces, members, audit, authorizer)


def get_collection_service(
    collections: Annotated[CollectionRepository, Depends(get_collection_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    authorizer: AuthorizerDep,
    settings: SettingsDep,
    indexing: IndexingServiceDep,
) -> CollectionService:
    return CollectionService(collections, audit, authorizer, settings, indexing)


def get_document_service(
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    chunks: Annotated[DocumentChunkRepository, Depends(get_chunk_repository)],
    collections: Annotated[CollectionRepository, Depends(get_collection_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    authorizer: AuthorizerDep,
    storage: Annotated[BlobStorage, Depends(get_storage)],
    settings: SettingsDep,
    indexing: IndexingServiceDep,
) -> DocumentService:
    return DocumentService(
        documents, chunks, collections, audit, authorizer, storage, settings, indexing
    )


def get_retrieval_service(
    chunks: Annotated[DocumentChunkRepository, Depends(get_chunk_repository)],
    collections: Annotated[CollectionRepository, Depends(get_collection_repository)],
    authorizer: AuthorizerDep,
    indexing: IndexingServiceDep,
    settings: SettingsDep,
) -> RetrievalService:
    return RetrievalService(chunks, collections, authorizer, indexing, settings)


def get_conversation_repository(session: SessionDep) -> ConversationRepository:
    return ConversationRepository(session)


def get_message_repository(session: SessionDep) -> MessageRepository:
    return MessageRepository(session)


def get_llm() -> ChatProvider:
    return get_chat_provider()


def get_answer_service(
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
    llm: Annotated[ChatProvider, Depends(get_llm)],
    settings: SettingsDep,
) -> AnswerService:
    return AnswerService(retrieval, llm, settings)


NamespaceServiceDep = Annotated[NamespaceService, Depends(get_namespace_service)]
CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
AnswerServiceDep = Annotated[AnswerService, Depends(get_answer_service)]


def get_chat_service(
    conversations: Annotated[ConversationRepository, Depends(get_conversation_repository)],
    messages: Annotated[MessageRepository, Depends(get_message_repository)],
    collections: Annotated[CollectionRepository, Depends(get_collection_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    authorizer: AuthorizerDep,
    answers: AnswerServiceDep,
    llm: Annotated[ChatProvider, Depends(get_llm)],
    settings: SettingsDep,
) -> ChatService:
    return ChatService(
        conversations, messages, collections, audit, authorizer, answers, llm, settings
    )


def get_job_repository(session: SessionDep) -> JobRepository:
    return JobRepository(session)


def get_lifecycle_service(
    jobs: Annotated[JobRepository, Depends(get_job_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    chunks: Annotated[DocumentChunkRepository, Depends(get_chunk_repository)],
    collections: Annotated[CollectionRepository, Depends(get_collection_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    authorizer: AuthorizerDep,
    indexing: IndexingServiceDep,
    settings: SettingsDep,
    processor: DocumentServiceDep,
    storage: Annotated[BlobStorage, Depends(get_storage)],
) -> DocumentLifecycleService:
    """The lifecycle service, with the ingestion pipeline as its processor.

    Composed here rather than inside either service, so neither has to import
    the other: ingestion stays unaware that a queue exists.
    """
    return DocumentLifecycleService(
        jobs,
        documents,
        chunks,
        collections,
        audit,
        authorizer,
        indexing,
        settings,
        processor,
        storage,
    )


LifecycleServiceDep = Annotated[DocumentLifecycleService, Depends(get_lifecycle_service)]


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


# --------------------------------------------------------------- principals ---
async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    auth_service: AuthServiceDep,
) -> User:
    """Resolve the bearer token to an active user."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("An Authorization: Bearer header is required.")
    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Unsupported authorization scheme.")
    return await auth_service.resolve_access_token(credentials.credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_namespace_context(
    namespace_id: Annotated[uuid.UUID, Path()],
    user: CurrentUser,
    authorizer: AuthorizerDep,
) -> NamespaceContext:
    """Verify membership of the namespace named in the path."""
    return await authorizer.authorize(user, namespace_id)


NamespaceContextDep = Annotated[NamespaceContext, Depends(get_namespace_context)]


def require_role(minimum: NamespaceRole):
    """Build a dependency that enforces a minimum namespace role."""

    async def dependency(context: NamespaceContextDep) -> NamespaceContext:
        context.require(minimum)
        return context

    return dependency
