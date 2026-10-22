"""Collection operations."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.db.models import AuditAction, AuditOutcome, Collection, NamespaceRole, User
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.schemas.collection import CollectionCreate, CollectionUpdate
from app.services.authorization import NamespaceAuthorizer, NamespaceContext
from app.services.indexing import IndexingService

logger = logging.getLogger(__name__)

#: Hard limit on a vector-store collection name (`collections.vector_collection`).
_VECTOR_NAME_LIMIT = 128


def build_vector_collection_name(
    namespace_slug: str, collection_slug: str, collection_id: uuid.UUID
) -> str:
    """Derive the backing vector-store collection name.

    Namespace-prefixed so the physical name says which tenant owns it, and
    suffixed with the collection's own id so the name is unique to this
    *instance* of the collection rather than to its slug.

    That suffix is what stops a deleted collection's leftover vectors from
    being inherited. Collections are soft-deleted, and if vector cleanup had
    failed - or had not run yet - a new collection reusing the slug
    "policies" would otherwise be handed the previous tenant-visible index,
    complete with documents the user believes they deleted.
    """
    suffix = collection_id.hex[:12]
    stem = f"ns-{namespace_slug}--{collection_slug}"
    # Truncate the stem, never the suffix: the suffix is what guarantees
    # uniqueness, and Chroma names are length-limited.
    stem = stem[: _VECTOR_NAME_LIMIT - len(suffix) - 2]
    return f"{stem}--{suffix}"


class CollectionService:
    def __init__(
        self,
        collections: CollectionRepository,
        audit: AuditRepository,
        authorizer: NamespaceAuthorizer,
        settings: Settings,
        indexing: IndexingService | None = None,
    ) -> None:
        self._collections = collections
        self._audit = audit
        self._authorizer = authorizer
        self._settings = settings
        # Optional, like DocumentService's: collections remain manageable
        # with no vector backend configured.
        self._indexing = indexing

    async def create(
        self, user: User, namespace_id: uuid.UUID, payload: CollectionCreate
    ) -> Collection:
        context = await self._authorizer.authorize(user, namespace_id)
        context.require_write()

        if await self._collections.get_by_slug(namespace_id, payload.slug):
            raise Conflict(
                f"A collection with the slug {payload.slug!r} already exists in this namespace."
            )

        # The width is a property of the model, not a setting to be chosen
        # alongside it, so both come from one resolved spec. Recording a
        # dimension that disagrees with the model would not fail until the
        # first document was embedded into an already-created collection.
        spec = self._settings.embedding_spec
        if payload.embedding_model and payload.embedding_model != spec.model:
            raise ValidationFailed(
                f"This deployment embeds with {spec.model!r}. A collection cannot request "
                f"{payload.embedding_model!r}: every vector in a collection must come from "
                "one model, and only the configured provider can produce them."
            )

        collection_id = uuid.uuid4()
        collection = Collection(
            id=collection_id,
            namespace_id=namespace_id,
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            vector_collection=build_vector_collection_name(
                context.namespace.slug, payload.slug, collection_id
            ),
            embedding_model=spec.model,
            embedding_dimensions=spec.dimensions,
            created_by_id=user.id,
            meta=payload.metadata,
        )
        await self._collections.add(collection)

        await self._audit.record(
            action=AuditAction.CREATE,
            event="collection.create",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="collection",
            resource_id=collection.id,
            resource_label=collection.slug,
        )
        return collection

    async def list_for_namespace(
        self, user: User, namespace_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Collection], int]:
        await self._authorizer.authorize(user, namespace_id)
        return await self._collections.list_for_namespace(namespace_id, limit=limit, offset=offset)

    async def get(
        self, user: User, namespace_id: uuid.UUID, collection_id: uuid.UUID
    ) -> Collection:
        await self._authorizer.authorize(user, namespace_id)
        return await self._require_collection(namespace_id, collection_id)

    async def update(
        self,
        user: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        payload: CollectionUpdate,
    ) -> Collection:
        context = await self._authorizer.authorize(user, namespace_id)
        context.require_write()

        collection = await self._require_collection(namespace_id, collection_id)
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise ValidationFailed("No fields to update.")

        for field, value in changes.items():
            # `metadata` is exposed under that name but stored on `meta`.
            setattr(collection, "meta" if field == "metadata" else field, value)

        await self._collections.flush()
        await self._audit.record(
            action=AuditAction.UPDATE,
            event="collection.update",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="collection",
            resource_id=collection.id,
            resource_label=collection.slug,
            metadata={"fields": sorted(changes)},
        )
        return collection

    async def delete(self, user: User, namespace_id: uuid.UUID, collection_id: uuid.UUID) -> None:
        # Deleting a collection retires every document in it, so it needs more
        # than the write permission that creating one does.
        context: NamespaceContext = await self._authorizer.authorize(
            user, namespace_id, minimum_role=NamespaceRole.ADMIN
        )
        collection = await self._require_collection(namespace_id, collection_id)

        collection.deleted_at = datetime.now(UTC)
        collection.is_active = False
        await self._collections.flush()

        # Best-effort, for the same reason as document deletion: retrieval
        # resolves every hit through the database, so the collection is
        # already unreachable. A failure here leaves an orphaned index to be
        # reconciled, not a privacy hole - and the vector collection name is
        # unique to this collection's id, so a future collection reusing the
        # slug will never be handed it.
        if self._indexing is not None:
            try:
                await self._indexing.remove_collection(collection)
            except Exception:
                logger.exception(
                    "Vector cleanup failed for deleted collection %s (%s)",
                    collection_id,
                    collection.vector_collection,
                )
                await self._audit.record(
                    action=AuditAction.DELETE,
                    event="collection.vectors.orphaned",
                    outcome=AuditOutcome.FAILURE,
                    actor_id=context.user.id,
                    actor_email=context.user.email,
                    namespace_id=namespace_id,
                    resource_type="collection",
                    resource_id=collection_id,
                    resource_label=collection.slug,
                    error_message="Vector cleanup failed; orphaned index needs reconciliation.",
                )

        await self._audit.record(
            action=AuditAction.DELETE,
            event="collection.delete",
            actor_id=context.user.id,
            actor_email=context.user.email,
            namespace_id=namespace_id,
            resource_type="collection",
            resource_id=collection_id,
            resource_label=collection.slug,
        )
        logger.info("Collection %s soft-deleted by %s", collection_id, user.id)

    async def _require_collection(
        self, namespace_id: uuid.UUID, collection_id: uuid.UUID
    ) -> Collection:
        collection = await self._collections.get(namespace_id, collection_id)
        if collection is None:
            # The repository scopes by namespace, so a collection belonging to
            # another tenant lands here and is reported as missing.
            raise NotFound("Collection not found.")
        return collection
