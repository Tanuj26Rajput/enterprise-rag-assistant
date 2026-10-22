"""In-memory repository doubles.

The API is exercised end to end over HTTP; only the SQL layer is replaced.
Authorization, validation, error mapping and the ingestion pipeline are the
real code, which is where the behaviour under test actually lives.

Each fake mirrors the predicates of the repository it stands in for -
including the namespace scoping and the soft-delete filters - because those
predicates *are* the tenant isolation. A fake that forgot them would make the
cross-tenant tests pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from app.db.models import (
    AuditAction,
    AuditLog,
    AuditOutcome,
    Collection,
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    IngestionJob,
    JobKind,
    JobStatus,
    Message,
    Namespace,
    NamespaceMember,
    NamespaceRole,
    User,
)


def materialize(instance: Any) -> Any:
    """Apply the column defaults a real INSERT would have applied.

    Transient ORM instances have no id or timestamps until they are flushed,
    so the fakes fill them in from the mapping metadata rather than from a
    hand-maintained list that could drift from the models.
    """
    mapper = sa.inspect(type(instance))
    for column in mapper.columns:
        key = mapper.get_property_by_column(column).key
        if getattr(instance, key, None) is not None:
            continue

        value: Any = None
        if column.default is not None:
            arg = column.default.arg
            if callable(arg):
                try:
                    value = arg()
                except TypeError:
                    value = arg(None)
            else:
                value = arg
        elif column.server_default is not None:
            rendered = str(column.server_default.arg).strip().lower()
            literal = rendered.strip("'")
            if "gen_random_uuid" in rendered:
                value = uuid.uuid4()
            elif "now()" in rendered:
                value = datetime.now(UTC)
            elif literal in {"true", "false"}:
                value = literal == "true"
            elif rendered.startswith("'{}'"):
                value = {}
            elif rendered.startswith("'[]'"):
                value = []
            elif literal.isdigit():
                value = int(literal)

        if value is not None:
            setattr(instance, key, value)
    return instance


@dataclass
class FakeStore:
    """The shared state behind every fake repository."""

    users: dict[uuid.UUID, User] = field(default_factory=dict)
    namespaces: dict[uuid.UUID, Namespace] = field(default_factory=dict)
    members: dict[uuid.UUID, NamespaceMember] = field(default_factory=dict)
    collections: dict[uuid.UUID, Collection] = field(default_factory=dict)
    documents: dict[uuid.UUID, Document] = field(default_factory=dict)
    chunks: dict[uuid.UUID, DocumentChunk] = field(default_factory=dict)
    conversations: dict[uuid.UUID, Conversation] = field(default_factory=dict)
    messages: dict[uuid.UUID, Message] = field(default_factory=dict)
    jobs: dict[uuid.UUID, IngestionJob] = field(default_factory=dict)
    audit: list[AuditLog] = field(default_factory=list)

    def events(self, event: str) -> list[AuditLog]:
        return [entry for entry in self.audit if entry.event == event]


class FakeSession:
    """Stands in for AsyncSession; the fakes hold the data, not a transaction."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None


class FakeUserRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._store.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        wanted = email.strip().lower()
        return next((u for u in self._store.users.values() if u.email == wanted), None)

    async def exists_with_email(self, email: str) -> bool:
        return await self.get_by_email(email) is not None

    async def add(self, user: User) -> User:
        materialize(user)
        self._store.users[user.id] = user
        return user


class FakeAuditRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def record(
        self,
        *,
        action: AuditAction,
        event: str,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        **fields: Any,
    ) -> AuditLog:
        metadata = fields.pop("metadata", None)
        entry = materialize(
            AuditLog(action=action, event=event, outcome=outcome, meta=metadata or {}, **fields)
        )
        self._store.audit.append(entry)
        return entry


class FakeNamespaceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def _live(self) -> list[Namespace]:
        return [n for n in self._store.namespaces.values() if n.deleted_at is None]

    async def get(self, namespace_id: uuid.UUID) -> Namespace | None:
        namespace = self._store.namespaces.get(namespace_id)
        return namespace if namespace and namespace.deleted_at is None else None

    async def get_by_slug(self, slug: str) -> Namespace | None:
        wanted = slug.strip().lower()
        return next((n for n in self._live() if n.slug == wanted), None)

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[tuple[Namespace, NamespaceRole]], int]:
        rows = [
            (self._store.namespaces[m.namespace_id], m.role)
            for m in self._store.members.values()
            if m.user_id == user_id
            and m.namespace_id in self._store.namespaces
            and self._store.namespaces[m.namespace_id].deleted_at is None
        ]
        rows.sort(key=lambda row: row[0].created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def add(self, namespace: Namespace) -> Namespace:
        materialize(namespace)
        self._store.namespaces[namespace.id] = namespace
        return namespace

    async def soft_delete(self, namespace: Namespace, *, at: Any) -> None:
        namespace.deleted_at = at


class FakeNamespaceMemberRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get(self, namespace_id: uuid.UUID, user_id: uuid.UUID) -> NamespaceMember | None:
        return next(
            (
                m
                for m in self._store.members.values()
                if m.namespace_id == namespace_id and m.user_id == user_id
            ),
            None,
        )

    async def get_by_id(
        self, namespace_id: uuid.UUID, member_id: uuid.UUID
    ) -> NamespaceMember | None:
        member = self._store.members.get(member_id)
        return member if member and member.namespace_id == namespace_id else None

    async def list_for_namespace(
        self, namespace_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[NamespaceMember], int]:
        rows = sorted(
            (m for m in self._store.members.values() if m.namespace_id == namespace_id),
            key=lambda m: m.created_at,
        )
        return rows[offset : offset + limit], len(rows)

    async def count_owners(self, namespace_id: uuid.UUID) -> int:
        return sum(
            1
            for m in self._store.members.values()
            if m.namespace_id == namespace_id and m.role is NamespaceRole.OWNER
        )

    async def add(self, member: NamespaceMember) -> NamespaceMember:
        materialize(member)
        self._store.members[member.id] = member
        return member

    async def remove(self, member: NamespaceMember) -> None:
        self._store.members.pop(member.id, None)

    async def find_user_by_email(self, email: str) -> User | None:
        wanted = email.strip().lower()
        return next((u for u in self._store.users.values() if u.email == wanted), None)


class FakeCollectionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def _live_in(self, namespace_id: uuid.UUID) -> list[Collection]:
        return [
            c
            for c in self._store.collections.values()
            if c.namespace_id == namespace_id and c.deleted_at is None
        ]

    async def get(self, namespace_id: uuid.UUID, collection_id: uuid.UUID) -> Collection | None:
        collection = self._store.collections.get(collection_id)
        if collection is None or collection.deleted_at is not None:
            return None
        # The namespace predicate is the cross-tenant guard.
        return collection if collection.namespace_id == namespace_id else None

    async def get_any(self, collection_id: uuid.UUID) -> Collection | None:
        return self._store.collections.get(collection_id)

    async def get_by_slug(self, namespace_id: uuid.UUID, slug: str) -> Collection | None:
        wanted = slug.strip().lower()
        return next((c for c in self._live_in(namespace_id) if c.slug == wanted), None)

    async def list_for_namespace(
        self, namespace_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Collection], int]:
        rows = sorted(self._live_in(namespace_id), key=lambda c: c.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def add(self, collection: Collection) -> Collection:
        materialize(collection)
        self._store.collections[collection.id] = collection
        return collection

    async def flush(self) -> None:
        return None


class FakeDocumentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get(self, namespace_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        document = self._store.documents.get(document_id)
        if document is None or document.deleted_at is not None:
            return None
        return document if document.namespace_id == namespace_id else None

    async def get_any(self, document_id: uuid.UUID) -> Document | None:
        # No namespace or soft-delete predicate: mirrors the real method,
        # which exists so a job can act on a row `get` deliberately hides.
        return self._store.documents.get(document_id)

    async def find_duplicate(self, collection_id: uuid.UUID, checksum: str) -> Document | None:
        return next(
            (
                d
                for d in self._store.documents.values()
                if d.collection_id == collection_id
                and d.checksum == checksum
                and d.deleted_at is None
            ),
            None,
        )

    async def list_for_collection(
        self,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: DocumentStatus | None = None,
    ) -> tuple[list[Document], int]:
        rows = [
            d
            for d in self._store.documents.values()
            if d.namespace_id == namespace_id
            and d.collection_id == collection_id
            and d.deleted_at is None
            and (status is None or d.status is status)
        ]
        rows.sort(key=lambda d: d.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def add(self, document: Document) -> Document:
        materialize(document)
        self._store.documents[document.id] = document
        return document

    async def flush(self) -> None:
        return None


class FakeDocumentChunkRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def replace_for_document(
        self, document_id: uuid.UUID, chunks: list[DocumentChunk]
    ) -> None:
        for chunk_id in [
            cid for cid, c in self._store.chunks.items() if c.document_id == document_id
        ]:
            del self._store.chunks[chunk_id]
        for chunk in chunks:
            materialize(chunk)
            self._store.chunks[chunk.id] = chunk

    async def list_for_document(
        self, document_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[DocumentChunk], int]:
        rows = sorted(
            (c for c in self._store.chunks.values() if c.document_id == document_id),
            key=lambda c: c.chunk_index,
        )
        return rows[offset : offset + limit], len(rows)

    async def get_by_vector_ids(
        self, namespace_id: uuid.UUID, vector_ids: list[str]
    ) -> dict[str, tuple[DocumentChunk, Document]]:
        """Mirrors the real query, including both of its predicates.

        The namespace check and the soft-delete check are the second half of
        retrieval's defence in depth. A fake that resolved every id would make
        the tests pass while leaving the leak untested.
        """
        if not vector_ids:
            return {}

        wanted = set(vector_ids)
        resolved: dict[str, tuple[DocumentChunk, Document]] = {}
        for chunk in self._store.chunks.values():
            if chunk.vector_id not in wanted:
                continue
            document = self._store.documents.get(chunk.document_id)
            if document is None:
                continue
            if document.namespace_id != namespace_id or document.deleted_at is not None:
                continue
            resolved[chunk.vector_id] = (chunk, document)
        return resolved


class FakeStorage:
    """Keeps uploaded bytes in a dict instead of on disk."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(
        self, *, namespace_id: uuid.UUID, document_id: uuid.UUID, extension: str, content: bytes
    ) -> str:
        uri = f"memory://{namespace_id}/{document_id}{extension}"
        self.blobs[uri] = content
        return uri

    async def get(self, uri: str) -> bytes:
        return self.blobs[uri]

    async def delete(self, uri: str) -> None:
        self.blobs.pop(uri, None)


class FakeChatProvider:
    """A scripted stand-in for the chat provider.

    Generation is the one part of this system whose output is genuinely
    non-deterministic, so it is the one part that has to be scripted rather
    than run for real. Everything *around* it - what reaches the prompt, how
    markers are resolved, what a failure turns into - is deterministic and is
    what these tests are actually about.
    """

    def __init__(self, model: str = "test/model") -> None:
        self._model = model
        #: Replies to serve, in order; the last one repeats once exhausted.
        self.replies: list[str] = ["A grounded answer [1]."]
        #: Set to raise instead of replying.
        self.error: Exception | None = None
        self.finish_reason: str = "stop"
        self.prompt_tokens: int | None = 120
        self.completion_tokens: int | None = 30
        #: Every request received, for asserting on what reached the model.
        self.requests: list[Any] = []

        # --- streaming ---
        #: Deltas to emit. None means "chop the scripted reply into words",
        #: which is close enough to how a real stream arrives.
        self.stream_chunks: list[str] | None = None
        #: Raised after ``fail_after_chunks`` deltas, to exercise a failure
        #: that happens once the status line is already 200.
        self.stream_error: Exception | None = None
        self.fail_after_chunks: int = 0
        #: Set when the consumer closes the generator early. This is the
        #: observable proof that a disconnect stops the upstream request.
        self.closed: bool = False
        #: Deltas actually yielded, to show a disconnect stopped the stream.
        self.emitted: list[str] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last_prompt(self) -> str:
        """The user turn of the most recent request."""
        return self.requests[-1].messages[-1].content

    def _next_reply(self) -> str:
        return self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]

    async def complete(self, request: Any) -> Any:
        from app.llm.base import ChatCompletion

        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ChatCompletion(
            content=self._next_reply(),
            model=self._model,
            finish_reason=self.finish_reason,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )

    async def stream(self, request: Any) -> Any:
        from app.llm.base import ChatChunk

        self.requests.append(request)
        if self.error is not None:
            raise self.error

        reply = self._next_reply()
        deltas = self.stream_chunks
        if deltas is None:
            # Word by word, keeping the spaces, so reassembling the deltas
            # reproduces the reply exactly.
            deltas = [part for part in re.split(r"(\s+)", reply) if part]

        try:
            for index, delta in enumerate(deltas):
                if self.stream_error is not None and index >= self.fail_after_chunks:
                    raise self.stream_error
                self.emitted.append(delta)
                yield ChatChunk(delta=delta)
                # Hand control back so a consumer that stops reading can
                # actually stop us; without this the whole stream would be
                # produced before the first await point.
                await asyncio.sleep(0)
            if self.stream_error is not None and len(deltas) >= self.fail_after_chunks:
                raise self.stream_error
        except GeneratorExit:
            self.closed = True
            raise
        finally:
            if not self.closed:
                self.closed = True

        yield ChatChunk(
            done=True,
            model=self._model,
            finish_reason=self.finish_reason,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


class FakeConversationRepository:
    """Mirrors the real repository's user *and* namespace predicates.

    Both matter: dropping the user predicate would make every
    conversation-isolation test pass while the application leaked threads
    between colleagues in the same namespace.
    """

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get(
        self, namespace_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None:
        conversation = self._store.conversations.get(conversation_id)
        if conversation is None:
            return None
        if conversation.namespace_id != namespace_id or conversation.user_id != user_id:
            return None
        if conversation.deleted_at is not None:
            return None
        return conversation

    async def list_for_user(
        self,
        namespace_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        include_archived: bool = False,
    ) -> tuple[list[Conversation], int]:
        rows = [
            c
            for c in self._store.conversations.values()
            if c.namespace_id == namespace_id
            and c.user_id == user_id
            and c.deleted_at is None
            and (include_archived or not c.is_archived)
        ]
        rows.sort(key=lambda c: c.last_message_at or c.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def add(self, conversation: Conversation) -> Conversation:
        materialize(conversation)
        self._store.conversations[conversation.id] = conversation
        return conversation

    async def flush(self) -> None:
        return None


class FakeMessageRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def _thread(self, conversation_id: uuid.UUID) -> list[Message]:
        return sorted(
            (m for m in self._store.messages.values() if m.conversation_id == conversation_id),
            key=lambda m: m.sequence,
        )

    async def list_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Message], int]:
        rows = self._thread(conversation_id)
        return rows[offset : offset + limit], len(rows)

    async def recent_turns(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        rows = self._thread(conversation_id)
        return rows[-limit:] if limit else []

    async def next_sequence(self, conversation_id: uuid.UUID) -> int:
        rows = self._thread(conversation_id)
        return rows[-1].sequence + 1 if rows else 0

    async def add(self, message: Message) -> Message:
        materialize(message)
        self._store.messages[message.id] = message
        return message

    async def flush(self) -> None:
        return None


class FakeJobRepository:
    """The ingestion queue, in a dict.

    The one thing this cannot reproduce is the part that matters most in
    production: ``SELECT ... FOR UPDATE SKIP LOCKED``. Concurrency between
    workers is a property of PostgreSQL, not of this code, so asserting it
    here would only be asserting my own model of the lock. What these tests
    do cover is everything built *on top* of claiming - attempt counting,
    backoff, dead-lettering, lease expiry and idempotent enqueueing - which
    is application logic and is where the bugs would be.
    """

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None:
        return self._store.jobs.get(job_id)

    async def active_for_document(
        self, document_id: uuid.UUID, kind: JobKind
    ) -> IngestionJob | None:
        return next(
            (
                job
                for job in self._store.jobs.values()
                if job.document_id == document_id
                and job.kind is kind
                and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            ),
            None,
        )

    async def list_for_document(
        self, document_id: uuid.UUID, *, limit: int = 20
    ) -> list[IngestionJob]:
        rows = [job for job in self._store.jobs.values() if job.document_id == document_id]
        rows.sort(key=lambda job: job.created_at, reverse=True)
        return rows[:limit]

    async def add(self, job: IngestionJob) -> IngestionJob:
        materialize(job)
        self._store.jobs[job.id] = job
        return job

    async def claim(self, *, owner: str, lease_seconds: int) -> IngestionJob | None:
        now = datetime.now(UTC)
        runnable = [
            job
            for job in self._store.jobs.values()
            if job.run_after <= now
            and (
                job.status is JobStatus.QUEUED
                or (
                    job.status is JobStatus.RUNNING
                    and (job.lease_expires_at is None or job.lease_expires_at < now)
                )
            )
        ]
        if not runnable:
            return None
        runnable.sort(key=lambda job: (job.run_after, job.created_at))
        job = runnable[0]
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.lease_owner = owner
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.started_at = job.started_at or now
        return job

    async def extend_lease(self, job: IngestionJob, *, lease_seconds: int) -> None:
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)

    async def mark_succeeded(self, job: IngestionJob) -> None:
        job.status = JobStatus.SUCCEEDED
        job.finished_at = datetime.now(UTC)
        job.last_error = None
        job.lease_owner = None
        job.lease_expires_at = None

    async def mark_failed(self, job: IngestionJob, error: str, *, retry_in: float | None) -> None:
        job.last_error = error[:2000]
        job.lease_owner = None
        job.lease_expires_at = None
        if retry_in is None or not job.can_retry:
            job.status = JobStatus.FAILED
            job.finished_at = datetime.now(UTC)
        else:
            job.status = JobStatus.QUEUED
            job.run_after = datetime.now(UTC) + timedelta(seconds=retry_in)

    async def release_expired(self, *, older_than_seconds: int = 0) -> int:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=older_than_seconds)
        stale = [
            job
            for job in self._store.jobs.values()
            if job.status is JobStatus.RUNNING
            and job.lease_expires_at is not None
            and job.lease_expires_at < cutoff
        ]
        for job in stale:
            job.lease_owner = None
            job.lease_expires_at = None
            if job.can_retry:
                job.status = JobStatus.QUEUED
                job.run_after = now
            else:
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.last_error = (
                    job.last_error or "The worker holding this job stopped without reporting."
                )
        return len(stale)

    async def counts_by_status(self, namespace_id: uuid.UUID | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self._store.jobs.values():
            if namespace_id is not None and job.namespace_id != namespace_id:
                continue
            counts[job.status.value] = counts.get(job.status.value, 0) + 1
        return counts

    async def flush(self) -> None:
        return None
