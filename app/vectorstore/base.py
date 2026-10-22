"""The vector store interface, and the identity scheme behind it.

Chroma is the only implementation today. It sits behind this interface so the
indexing and retrieval services never import it, which is what makes swapping
in pgvector or Qdrant a matter of writing one class.

**Stable identifiers.** A chunk's vector id is ``{document_id}:{chunk_index}``
and nothing else. It is derived, not generated, which is what makes the whole
pipeline idempotent: re-indexing a document computes the same ids and upserts
over the same rows, so a retry after a crash, a timeout or a partial write
converges on the same state instead of duplicating it. A random id per
attempt would leave the previous attempt's vectors behind as undetectable
duplicates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

#: Metadata keys written on every vector. Namespace and collection are
#: denormalised onto each one so a query can filter by tenant inside the
#: store, rather than fetching and discarding another tenant's rows.
META_NAMESPACE_ID = "namespace_id"
META_COLLECTION_ID = "collection_id"
META_DOCUMENT_ID = "document_id"
META_CHUNK_INDEX = "chunk_index"
META_CHUNK_ID = "chunk_id"
META_PAGE_NUMBER = "page_number"
META_CONTENT_HASH = "content_hash"


def vector_id_for(document_id: uuid.UUID, chunk_index: int) -> str:
    """The stable id of one chunk's vector."""
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    return f"{document_id}:{chunk_index}"


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """One chunk, ready to be written to the store."""

    id: str
    embedding: list[float]
    document: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One retrieval hit."""

    id: str
    score: float
    document: str
    metadata: dict[str, Any]

    @property
    def document_id(self) -> uuid.UUID | None:
        raw = self.metadata.get(META_DOCUMENT_ID)
        try:
            return uuid.UUID(str(raw))
        except (TypeError, ValueError):
            return None

    @property
    def chunk_index(self) -> int | None:
        raw = self.metadata.get(META_CHUNK_INDEX)
        return int(raw) if isinstance(raw, int | float | str) and str(raw).isdigit() else None


class VectorStore(Protocol):
    """Where embeddings live."""

    async def ensure_collection(self, name: str, *, embedding_model: str, dimensions: int) -> None:
        """Create the collection if absent; verify its shape if present."""
        ...

    async def upsert(self, name: str, records: list[VectorRecord]) -> None:
        """Write records, overwriting any that share an id."""
        ...

    async def query(
        self,
        name: str,
        *,
        embedding: list[float],
        namespace_id: uuid.UUID,
        top_k: int,
        collection_id: uuid.UUID | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[VectorMatch]:
        """Nearest neighbours, restricted to one namespace."""
        ...

    async def delete_document(self, name: str, document_id: uuid.UUID) -> int:
        """Remove every vector belonging to one document."""
        ...

    async def delete_stale_chunks(
        self, name: str, document_id: uuid.UUID, *, keep_through_index: int
    ) -> int:
        """Remove a document's vectors at or beyond ``keep_through_index``.

        Re-processing can produce fewer chunks than last time - an edited
        document, a changed chunk size. Upserting alone would leave the
        surplus tail in place, still searchable and now citing text the
        document no longer contains.
        """
        ...

    async def delete_ids(self, name: str, ids: list[str]) -> int:
        """Remove specific vectors by id.

        Used by reconciliation, which decides what is stale by comparing the
        store against the database rather than by any predicate the store can
        express.
        """
        ...

    async def list_ids(self, name: str, *, limit: int, offset: int) -> list[str]:
        """A page of vector ids, for sweeping the whole collection.

        Paged rather than returned whole: a reconciliation pass over a large
        index must not depend on the whole id set fitting in memory.
        """
        ...

    async def delete_collection(self, name: str) -> None:
        """Drop the whole collection. Succeeds if it is already gone."""
        ...

    async def count(self, name: str) -> int:
        """Number of vectors in the collection; 0 if it does not exist."""
        ...

    async def heartbeat(self) -> bool:
        """Can the store be reached right now?

        For the readiness probe. Deliberately not a query against a real
        collection: readiness must not depend on a tenant's data existing,
        and a probe that reads tenant data would run on every scrape.
        """
        ...


class VectorStoreError(RuntimeError):
    """The vector store could not satisfy a request."""


class VectorCollectionMismatch(VectorStoreError):
    """An existing collection does not match the embedding configuration.

    Mixing widths or models inside one collection makes distances
    meaningless, so this is refused rather than reconciled.
    """
