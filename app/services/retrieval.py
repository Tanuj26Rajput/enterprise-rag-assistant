"""Search over a collection's indexed chunks.

Retrieval runs the tenant predicate twice, in two different systems, and that
redundancy is deliberate:

1. The vector store filters by ``namespace_id`` inside the query, so another
   tenant's text is never fetched into this process at all.
2. Every hit is then resolved back through the database, which re-applies the
   namespace predicate and the soft-delete filter on ``documents``.

Either check alone would be defensible. Together they mean that a stale
vector, a collection-name collision, or a bug in the store's filter cannot
put one tenant's text in another's results - and that a deleted document
stops being citable the moment the row is marked deleted, whether or not its
vectors have been cleaned up yet.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import NotFound, ServiceUnavailable, ValidationFailed
from app.core.metrics import record_retrieval
from app.db.models import Document, DocumentChunk, User
from app.repositories.collection import CollectionRepository
from app.repositories.document import DocumentChunkRepository
from app.services.authorization import NamespaceAuthorizer, require_active_collection
from app.services.indexing import IndexingError, IndexingService

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 4_000


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One hit, carrying everything a citation needs."""

    chunk: DocumentChunk
    document: Document
    score: float


class RetrievalService:
    def __init__(
        self,
        chunks: DocumentChunkRepository,
        collections: CollectionRepository,
        authorizer: NamespaceAuthorizer,
        indexing: IndexingService,
        settings: Settings,
    ) -> None:
        self._chunks = chunks
        self._collections = collections
        self._authorizer = authorizer
        self._indexing = indexing
        self._settings = settings

    async def search(
        self,
        user: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        query: str,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[RetrievedChunk]:
        # Membership of the namespace is required to search it. A non-member
        # gets 404 from the authorizer, not 403, for the same anti-enumeration
        # reason as every other namespace-scoped route.
        await self._authorizer.authorize(user, namespace_id)

        query = query.strip()
        if not query:
            raise ValidationFailed("A search query is required.")
        if len(query) > MAX_QUERY_LENGTH:
            raise ValidationFailed(f"A search query may be at most {MAX_QUERY_LENGTH} characters.")

        collection = await self._collections.get(namespace_id, collection_id)
        if collection is None:
            raise NotFound("Collection not found.")
        require_active_collection(collection)

        limit = top_k or self._settings.retrieval_top_k
        started = time.perf_counter()
        try:
            matches = await self._indexing.search(
                query=query,
                collection=collection,
                namespace_id=namespace_id,
                top_k=limit,
                min_score=min_score,
            )
        except IndexingError as exc:
            # The embedding provider or the vector store is down, or the
            # collection was built with a model this deployment no longer
            # has. None of that is the caller's fault, and none of it should
            # surface as an unhandled 500 with the provider's wording in it.
            logger.warning("Retrieval failed for namespace %s: %s", namespace_id, exc)
            raise ServiceUnavailable(
                "Search is temporarily unavailable. Please try again."
            ) from exc
        # Timed here rather than around the whole method: this is the span
        # that embeds the query and hits the index, and it is the one whose
        # latency a slow day is actually asking about.
        elapsed = time.perf_counter() - started
        if not matches:
            record_retrieval(0, elapsed)
            return []

        resolved = await self._chunks.get_by_vector_ids(
            namespace_id, [match.id for match in matches]
        )

        results: list[RetrievedChunk] = []
        for match in matches:
            found = resolved.get(match.id)
            if found is None:
                # The index is ahead of the database: the document was
                # deleted, or its chunks were replaced by a re-process whose
                # vector cleanup has not landed. Dropping the hit is correct;
                # surfacing text the database no longer has is not.
                logger.debug("Dropping unresolvable vector hit %s", match.id)
                continue
            chunk, document = found
            results.append(RetrievedChunk(chunk=chunk, document=document, score=match.score))

        if len(results) < len(matches):
            logger.info(
                "Dropped %d of %d vector hit(s) with no live chunk in namespace %s",
                len(matches) - len(results),
                len(matches),
                namespace_id,
            )
        # The count reported is what the caller can actually use, after
        # unresolvable hits are dropped. Reporting the raw index count would
        # hide exactly the drift this drop exists to absorb.
        record_retrieval(len(results), elapsed)
        return results
