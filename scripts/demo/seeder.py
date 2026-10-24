"""Apply the demo catalogue through the application's own services.

The rule this module follows throughout: **nothing is written by hand.** Every
account goes through :class:`~app.services.auth.AuthService`, every workspace
through :class:`~app.services.namespace.NamespaceService`, every document
through :class:`~app.services.document.DocumentService` and the lifecycle
service that owns ingestion. The seeder composes those services exactly as
``app/api/deps.py`` composes them for a request, and then calls them.

That matters for more than tidiness. Passwords are hashed by the same
function the login path verifies against; authorization is checked on every
call, so a seeded membership is a membership the API would have granted;
chunking, embedding and vector-store writes are the production code path, so
the demo corpus is indexed the way a real upload would be.

Two properties are enforced here rather than inherited:

*Idempotent.* Every step looks for what it would create and skips it if it is
already there. Re-running the seeder adds nothing and changes nothing.

*Never destructive.* There is no code path in this module that deletes or
overwrites anything. An object that exists is left exactly as it is, even
when it differs from the catalogue.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.db.models import User
from app.db.models.enums import DocumentStatus, JobKind, JobStatus
from app.embeddings.factory import get_embedding_provider
from app.llm.factory import LazyChatProvider
from app.llm.prompts import REFUSAL_MESSAGE
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.repositories.conversation import ConversationRepository, MessageRepository
from app.repositories.document import DocumentChunkRepository, DocumentRepository
from app.repositories.job import JobRepository
from app.repositories.namespace import NamespaceMemberRepository, NamespaceRepository
from app.repositories.user import UserRepository
from app.schemas.collection import CollectionCreate
from app.schemas.namespace import NamespaceCreate
from app.services.auth import AuthService
from app.services.authorization import NamespaceAuthorizer
from app.services.chat import ChatService, StreamOutcome
from app.services.collection import CollectionService
from app.services.document import DocumentService
from app.services.generation import AnswerService
from app.services.indexing import IndexingService
from app.services.lifecycle import DocumentLifecycleService
from app.services.namespace import NamespaceService
from app.services.retrieval import RetrievalService
from app.services.storage import LocalFileStorage
from app.vectorstore.chroma import ChromaVectorStore
from scripts.demo import catalog
from scripts.demo.rendering import render

logger = logging.getLogger("seed")


@dataclass(slots=True)
class Report:
    """What the run did, for the summary printed at the end."""

    users_created: list[str] = field(default_factory=list)
    users_existing: list[str] = field(default_factory=list)
    namespaces_created: list[str] = field(default_factory=list)
    namespaces_existing: list[str] = field(default_factory=list)
    memberships_created: list[str] = field(default_factory=list)
    memberships_existing: list[str] = field(default_factory=list)
    collections_created: list[str] = field(default_factory=list)
    collections_existing: list[str] = field(default_factory=list)
    documents_created: list[str] = field(default_factory=list)
    documents_existing: list[str] = field(default_factory=list)
    documents_failed: list[str] = field(default_factory=list)
    conversations_created: list[str] = field(default_factory=list)
    conversations_existing: list[str] = field(default_factory=list)
    conversations_failed: list[str] = field(default_factory=list)

    @property
    def anything_failed(self) -> bool:
        return bool(self.documents_failed or self.conversations_failed)


class DemoSeeder:
    """Composes the application's services and applies the catalogue."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

        # ---- repositories: one session, exactly as a request would have ----
        users = UserRepository(session)
        audit = AuditRepository(session)
        namespaces = NamespaceRepository(session)
        members = NamespaceMemberRepository(session)
        collections = CollectionRepository(session)
        documents = DocumentRepository(session)
        chunks = DocumentChunkRepository(session)
        jobs = JobRepository(session)
        conversations = ConversationRepository(session)
        messages = MessageRepository(session)

        self._users = users
        self._namespaces = namespaces
        self._members = members
        self._collections = collections
        self._documents = documents

        # ---- adapters ----
        storage = LocalFileStorage(settings.upload_dir)
        store = ChromaVectorStore(
            persist_dir=settings.chroma_persist_dir,
            host=settings.chroma_host,
            port=settings.chroma_port,
            distance=settings.chroma_distance,
        )
        embeddings = get_embedding_provider()
        indexing = IndexingService(store, embeddings, settings)

        # ---- services, in deps.py's order ----
        authorizer = NamespaceAuthorizer(namespaces, members)
        self._auth = AuthService(users, audit, settings)
        self._namespace_service = NamespaceService(namespaces, members, audit, authorizer)
        self._collection_service = CollectionService(
            collections, audit, authorizer, settings, indexing
        )
        self._document_service = DocumentService(
            documents, chunks, collections, audit, authorizer, storage, settings, indexing
        )
        self._lifecycle = DocumentLifecycleService(
            jobs,
            documents,
            chunks,
            collections,
            audit,
            authorizer,
            indexing,
            settings,
            self._document_service,
            storage,
        )

        # The chat provider is built lazily, so composing the seeder costs
        # nothing when conversations are not being seeded and no model
        # credential is configured.
        llm = LazyChatProvider()
        retrieval = RetrievalService(chunks, collections, authorizer, indexing, settings)
        answers = AnswerService(retrieval, llm, settings)
        self._chat = ChatService(
            conversations, messages, collections, audit, authorizer, answers, llm, settings
        )

        self.report = Report()

    # ----------------------------------------------------------- accounts ---
    async def seed_users(self) -> dict[str, User]:
        """Register the demo accounts, or adopt the ones already present."""
        resolved: dict[str, User] = {}

        for demo in catalog.USERS:
            existing = await self._users.get_by_email(demo.email)
            if existing is not None:
                # Left exactly as found. In particular the password is *not*
                # reset: an existing account may be one the operator cares
                # about, and silently changing its credentials would be a
                # destructive act dressed up as a convenience.
                resolved[demo.email] = existing
                self.report.users_existing.append(demo.email)
                logger.info("user %s already exists; left unchanged", demo.email)
                continue

            user = await self._auth.register(
                email=demo.email,
                password=demo.password,
                full_name=demo.full_name,
            )
            if demo.is_superuser:
                # Registration has no affordance for this, by design: there
                # is no HTTP route that mints a platform operator. Setting it
                # here is the one place the seeder writes a column directly,
                # and it is why the script refuses to run against production.
                user.is_superuser = True

            await self._session.commit()
            resolved[demo.email] = user
            self.report.users_created.append(demo.email)
            logger.info("created user %s", demo.email)

        return resolved

    # --------------------------------------------------------- workspaces ---
    async def seed_namespaces(self, users: dict[str, User]) -> None:
        admin = users[catalog.ADMIN.email]

        for demo_ns in catalog.NAMESPACES:
            namespace = await self._namespaces.get_by_slug(demo_ns.slug)

            if namespace is None:
                namespace, _ = await self._namespace_service.create(
                    admin,
                    NamespaceCreate(
                        slug=demo_ns.slug,
                        name=demo_ns.name,
                        description=demo_ns.description,
                    ),
                )
                await self._session.commit()
                self.report.namespaces_created.append(demo_ns.slug)
                logger.info("created namespace %s", demo_ns.slug)
            else:
                self.report.namespaces_existing.append(demo_ns.slug)
                logger.info("namespace %s already exists; reusing it", demo_ns.slug)

            await self._seed_members(admin, namespace.id, demo_ns)
            await self._seed_collection(admin, namespace.id, demo_ns)

    async def _seed_members(
        self, admin: User, namespace_id: uuid.UUID, demo_ns: catalog.DemoNamespace
    ) -> None:
        for demo_user, role in demo_ns.members:
            invitee = await self._users.get_by_email(demo_user.email)
            if invitee is None:
                logger.warning(
                    "cannot add %s to %s: the account does not exist",
                    demo_user.email,
                    demo_ns.slug,
                )
                continue

            label = f"{demo_user.email} -> {demo_ns.slug} ({role.value})"
            if await self._members.get(namespace_id, invitee.id) is not None:
                # Note what is *not* happening: an existing membership keeps
                # whatever role it has. Re-running the seeder must never
                # change someone's access.
                self.report.memberships_existing.append(label)
                continue

            await self._namespace_service.add_member(
                admin, namespace_id, email=demo_user.email, role=role
            )
            await self._session.commit()
            self.report.memberships_created.append(label)
            logger.info("added %s", label)

    async def _seed_collection(
        self, admin: User, namespace_id: uuid.UUID, demo_ns: catalog.DemoNamespace
    ) -> None:
        demo_col = demo_ns.collection
        label = f"{demo_ns.slug}/{demo_col.slug}"

        collection = await self._collections.get_by_slug(namespace_id, demo_col.slug)
        if collection is None:
            collection = await self._collection_service.create(
                admin,
                namespace_id,
                CollectionCreate(
                    slug=demo_col.slug,
                    name=demo_col.name,
                    description=demo_col.description,
                ),
            )
            await self._session.commit()
            self.report.collections_created.append(label)
            logger.info("created collection %s", label)
        else:
            self.report.collections_existing.append(label)

        await self._seed_documents(admin, namespace_id, collection.id, demo_ns)

    # ---------------------------------------------------------- documents ---
    async def _seed_documents(
        self,
        admin: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        demo_ns: catalog.DemoNamespace,
    ) -> None:
        present = await self._filenames_in(namespace_id, collection_id)

        for demo_doc in demo_ns.collection.documents:
            label = f"{demo_ns.slug}/{demo_ns.collection.slug}/{demo_doc.filename}"

            if demo_doc.filename in present:
                # Deduplicated by filename rather than by content hash. The
                # pipeline's own hash dedup is still in force underneath, but
                # it cannot be relied on here: a PDF writer embeds state that
                # varies between runs, so the same source can render to
                # different bytes and would slip past a content check.
                self.report.documents_existing.append(label)
                continue

            try:
                content, content_type = render(
                    demo_doc.source_path.read_text(encoding="utf-8"),
                    fmt=demo_doc.fmt,
                    title=demo_doc.title,
                )

                document = await self._document_service.upload(
                    admin,
                    namespace_id,
                    collection_id,
                    filename=demo_doc.filename,
                    content_type=content_type,
                    content=content,
                    title=demo_doc.title,
                )
                # Commit the upload before ingestion runs. The document row
                # and its job are durable from this point, so a failure
                # during extraction leaves a `failed` document to look at
                # rather than nothing at all - the same guarantee the HTTP
                # upload path gives.
                await self._session.commit()

                _, result = await self._lifecycle.enqueue_and_maybe_run(document, JobKind.INGEST)
                await self._session.commit()

            except (AppError, OSError, ValueError) as exc:
                # One document failing must not cost the other sixteen. The
                # session is rolled back to a clean state and the run
                # continues; the failure is reported at the end.
                await self._session.rollback()
                self.report.documents_failed.append(f"{label}: {exc}")
                logger.error("failed to seed %s: %s", label, exc)
                continue

            if document.status is DocumentStatus.FAILED:
                self.report.documents_failed.append(
                    f"{label}: ingestion failed - {document.error_message}"
                )
                logger.error("ingestion failed for %s: %s", label, document.error_message)
                continue

            self.report.documents_created.append(label)
            logger.info(
                "seeded %s (%s, %d chunks)",
                label,
                document.status.value,
                document.chunk_count,
            )
            if result is not None and result.status is not JobStatus.SUCCEEDED:
                logger.warning("ingestion job for %s ended as %s", label, result.status.value)

    async def _filenames_in(self, namespace_id: uuid.UUID, collection_id: uuid.UUID) -> set[str]:
        names: set[str] = set()
        offset = 0
        while True:
            rows, total = await self._documents.list_for_collection(
                namespace_id, collection_id, limit=100, offset=offset
            )
            names.update(row.filename for row in rows)
            offset += len(rows)
            if not rows or offset >= total:
                return names

    # ------------------------------------------------------ conversations ---
    async def seed_conversations(self, users: dict[str, User]) -> None:
        """Run the real question-and-answer path for each demo scenario.

        Opt-in, because this is the only part of seeding that calls a chat
        model and therefore the only part that costs money. The alternative -
        writing answers and citations into the catalogue - was rejected: a
        fabricated citation in a demo of a system whose entire premise is
        that citations cannot be fabricated would be the worst possible thing
        to ship.
        """
        for scenario in catalog.SCENARIOS:
            namespace = await self._namespaces.get_by_slug(scenario.namespace.slug)
            if namespace is None:
                logger.warning(
                    "skipping scenario %r: namespace %s is missing",
                    scenario.title,
                    scenario.namespace.slug,
                )
                continue

            collection = await self._collections.get_by_slug(
                namespace.id, scenario.namespace.collection.slug
            )
            if collection is None:
                logger.warning(
                    "skipping scenario %r: collection %s is missing",
                    scenario.title,
                    scenario.namespace.collection.slug,
                )
                continue

            asker = users.get(scenario.asked_by.email)
            if asker is None:
                logger.warning("skipping scenario %r: asker is missing", scenario.title)
                continue

            label = f"{scenario.namespace.slug}: {scenario.title}"
            if await self._conversation_exists(asker, namespace.id, scenario.title):
                self.report.conversations_existing.append(label)
                continue

            try:
                await self._run_scenario(asker, namespace.id, collection.id, scenario)
            except Exception as exc:  # noqa: BLE001 - reported at the end, not swallowed
                await self._session.rollback()
                self.report.conversations_failed.append(f"{label}: {exc}")
                logger.error("failed to seed conversation %r: %s", label, exc)
                continue

            self.report.conversations_created.append(label)
            logger.info("seeded conversation %s", label)

    async def _conversation_exists(
        self, asker: User, namespace_id: uuid.UUID, title: str
    ) -> bool:
        existing, _ = await self._chat.list_for_user(
            asker, namespace_id, limit=100, offset=0, include_archived=True
        )
        return any(conversation.title == title for conversation in existing)

    async def _run_scenario(
        self,
        asker: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        scenario: catalog.DemoScenario,
    ) -> None:
        conversation = await self._chat.create(
            asker, namespace_id, collection_id=collection_id, title=scenario.title
        )
        await self._session.commit()

        for question in scenario.questions:
            # Exactly the sequence the SSE endpoint follows: prepare the
            # evidence, make the question durable, stream the answer, then
            # persist the turn. The stream is consumed and discarded - there
            # is no client here - but it is a real stream, so the stored
            # answer and its citations are the ones a user would have seen.
            context = await self._chat.prepare_turn(
                asker, namespace_id, conversation, question=question
            )
            await self._chat.record_question(conversation, context.question)
            await self._session.commit()

            outcome = StreamOutcome(conversation_id=conversation.id, question=context.question)

            if context.is_empty:
                # No evidence retrieved. The endpoint refuses in-band here
                # without calling the model, and so does this: sending a
                # prompt with nothing to ground it would spend tokens to
                # produce an answer the system is designed not to give.
                outcome.text = REFUSAL_MESSAGE
                outcome.refused = True
                outcome.finish_reason = "no_evidence"
            else:
                async for _delta in self._chat.stream_turn(context, outcome):
                    pass

            await self._chat.record_answer(conversation, outcome)
            await self._session.commit()

            logger.info(
                "  %s -> %d citation(s)%s",
                question[:60],
                len(outcome.citations),
                "" if not outcome.refused else " (insufficient evidence)",
            )
