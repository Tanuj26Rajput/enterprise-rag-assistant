"""Embedding chunks into the vector store, and taking them out again.

The database is the system of record. The vector store is a derived index:
anything in it can be rebuilt from ``document_chunks``, and nothing is only
in it. That ordering decides how every failure here is handled.

**Idempotence.** Vector ids are derived from ``(document_id, chunk_index)``,
so re-indexing a document writes over the same rows. ``index_document`` can
therefore be retried after any failure - a crash, a timeout, a half-finished
batch - and converges on the correct state rather than accumulating
duplicates. The only operation that is not a plain overwrite is pruning the
tail, which is why it is ordered last: vectors are written first, and only
once they are safely in place is the surplus from a previous, longer version
of the document removed. The reverse order would leave a window where a
crash loses chunks that the database still claims are indexed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.models import Collection, Document, DocumentChunk
from app.embeddings.base import EmbeddingError, EmbeddingProvider
from app.vectorstore.base import (
    META_CHUNK_ID,
    META_CHUNK_INDEX,
    META_COLLECTION_ID,
    META_CONTENT_HASH,
    META_DOCUMENT_ID,
    META_NAMESPACE_ID,
    META_PAGE_NUMBER,
    VectorMatch,
    VectorRecord,
    VectorStore,
    VectorStoreError,
    vector_id_for,
)

logger = logging.getLogger(__name__)


class IndexingError(RuntimeError):
    """Indexing failed; the document must not be reported as ready."""


class IndexingService:
    def __init__(
        self,
        store: VectorStore,
        embeddings: EmbeddingProvider,
        settings: Settings,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._settings = settings

    # -------------------------------------------------------------- write ---
    async def index_document(
        self,
        document: Document,
        collection: Collection,
        chunks: list[DocumentChunk],
    ) -> int:
        """Embed and store every chunk of one document.

        Returns the number of vectors written. Raises :class:`IndexingError`
        on any failure, leaving the caller to mark the document failed - a
        document whose text is in the database but whose vectors are not is
        invisible to retrieval, so it must not be called ready.
        """
        self._assert_compatible(collection)

        if not chunks:
            # No chunks is a legitimate end state for a re-processed document
            # that now yields nothing; its old vectors still have to go.
            await self._store.delete_document(collection.vector_collection, document.id)
            return 0

        try:
            await self._store.ensure_collection(
                collection.vector_collection,
                embedding_model=collection.embedding_model,
                dimensions=collection.embedding_dimensions,
            )

            vectors = await self._embeddings.embed_documents([c.content for c in chunks])

            records = [
                VectorRecord(
                    id=vector_id_for(document.id, chunk.chunk_index),
                    embedding=vector,
                    document=chunk.content,
                    metadata={
                        # Denormalised so the store can filter by tenant
                        # without consulting the database.
                        META_NAMESPACE_ID: str(document.namespace_id),
                        META_COLLECTION_ID: str(document.collection_id),
                        META_DOCUMENT_ID: str(document.id),
                        META_CHUNK_ID: str(chunk.id),
                        META_CHUNK_INDEX: chunk.chunk_index,
                        META_PAGE_NUMBER: chunk.page_number,
                        META_CONTENT_HASH: chunk.content_hash,
                    },
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]

            await self._store.upsert(collection.vector_collection, records)

            # Only now that the new vectors are durable: anything beyond the
            # last index belongs to a longer previous version of this
            # document and would otherwise stay searchable forever.
            pruned = await self._store.delete_stale_chunks(
                collection.vector_collection,
                document.id,
                keep_through_index=len(chunks),
            )
            if pruned:
                logger.info(
                    "Pruned %d stale vector(s) from document %s after re-indexing",
                    pruned,
                    document.id,
                )

        except (EmbeddingError, VectorStoreError) as exc:
            raise IndexingError(str(exc)) from exc

        embedded_at = datetime.now(UTC)
        for chunk, record in zip(chunks, records, strict=True):
            chunk.vector_id = record.id
            chunk.embedded_at = embedded_at

        logger.info(
            "Indexed %d chunk(s) of document %s into %s",
            len(records),
            document.id,
            collection.vector_collection,
        )
        return len(records)

    # ------------------------------------------------------------ cleanup ---
    async def remove_document(self, collection: Collection, document_id: uuid.UUID) -> int:
        """Delete one document's vectors. Safe to call repeatedly."""
        removed = await self._store.delete_document(collection.vector_collection, document_id)
        logger.info("Removed %d vector(s) for document %s", removed, document_id)
        return removed

    async def remove_vectors(self, collection: Collection, vector_ids: list[str]) -> int:
        """Delete specific vectors. Used by reconciliation, not by ingestion."""
        if not vector_ids:
            return 0
        try:
            return await self._store.delete_ids(collection.vector_collection, vector_ids)
        except VectorStoreError as exc:
            raise IndexingError(str(exc)) from exc

    async def list_vector_ids(
        self, collection: Collection, *, limit: int, offset: int
    ) -> list[str]:
        """A page of the ids this collection's index actually holds."""
        try:
            return await self._store.list_ids(
                collection.vector_collection, limit=limit, offset=offset
            )
        except VectorStoreError as exc:
            raise IndexingError(str(exc)) from exc

    async def remove_collection(self, collection: Collection) -> None:
        """Drop a collection's whole vector index. Safe to call repeatedly."""
        await self._store.delete_collection(collection.vector_collection)
        logger.info("Dropped vector collection %s", collection.vector_collection)

    # --------------------------------------------------------------- read ---
    async def search(
        self,
        *,
        query: str,
        collection: Collection,
        namespace_id: uuid.UUID,
        top_k: int,
        min_score: float | None = None,
    ) -> list[VectorMatch]:
        """Nearest chunks to ``query``, restricted to one namespace."""
        self._assert_compatible(collection)

        try:
            embedding = await self._embeddings.embed_query(query)
            matches = await self._store.query(
                collection.vector_collection,
                embedding=embedding,
                namespace_id=namespace_id,
                collection_id=collection.id,
                top_k=top_k,
            )
        except (EmbeddingError, VectorStoreError) as exc:
            raise IndexingError(str(exc)) from exc

        if min_score is not None:
            matches = [match for match in matches if match.score >= min_score]
        return matches

    # -------------------------------------------------------------- guards ---
    def _assert_compatible(self, collection: Collection) -> None:
        """Refuse to mix embedding spaces within one collection.

        A collection's vectors were produced by one model at one width. If
        the deployment's configuration has since changed, queries would be
        embedded by a different model than the index - returning confident
        nonsense rather than an error.
        """
        spec = self._embeddings.spec
        if collection.embedding_model != spec.model:
            raise IndexingError(
                f"Collection {collection.slug!r} was built with {collection.embedding_model!r} "
                f"but the configured embedding model is {spec.model!r}. Re-embed the "
                "collection before using it."
            )
        if collection.embedding_dimensions != spec.dimensions:
            raise IndexingError(
                f"Collection {collection.slug!r} holds "
                f"{collection.embedding_dimensions}-dimensional vectors but "
                f"{spec.model!r} is configured for {spec.dimensions}."
            )
