"""Persistent ChromaDB implementation of :class:`VectorStore`.

Two deployment shapes, chosen by configuration: an embedded client writing to
a local directory (development, single-process), and an HTTP client against a
Chroma server (anything with more than one worker, since the embedded client
owns its files exclusively and a second process would corrupt or block them).

Chroma's Python client is synchronous, so every call is pushed to a worker
thread. Running it inline would block the event loop for the duration of an
HNSW search, stalling every other request on the worker.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import chromadb
from chromadb.api import ClientAPI
from chromadb.errors import ChromaError, NotFoundError

from app.vectorstore.base import (
    META_CHUNK_INDEX,
    META_COLLECTION_ID,
    META_DOCUMENT_ID,
    META_NAMESPACE_ID,
    VectorCollectionMismatch,
    VectorMatch,
    VectorRecord,
    VectorStoreError,
)

logger = logging.getLogger(__name__)

#: Collection metadata keys recording what the vectors were produced by, so a
#: configuration change is caught on the next write instead of silently
#: mixing two embedding spaces in one index.
_META_MODEL = "embedding_model"
_META_DIMENSIONS = "embedding_dimensions"


class ChromaVectorStore:
    """Vector storage backed by a persistent Chroma deployment."""

    def __init__(
        self,
        *,
        persist_dir: str | None = None,
        host: str | None = None,
        port: int = 8000,
        distance: str = "cosine",
    ) -> None:
        self._distance = distance
        self._host = host
        self._port = port
        self._persist_dir = persist_dir
        self._client: ClientAPI | None = None
        # Chroma's client is not documented as thread-safe for concurrent
        # schema changes; this serialises get_or_create across requests.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- client ---
    def _connect(self) -> ClientAPI:
        if self._host:
            logger.info("Chroma: HTTP client at %s:%s", self._host, self._port)
            return chromadb.HttpClient(host=self._host, port=self._port)
        if not self._persist_dir:
            raise VectorStoreError("Chroma needs CHROMA_PERSIST_DIR or CHROMA_HOST.")
        logger.info("Chroma: persistent client at %s", self._persist_dir)
        return chromadb.PersistentClient(path=self._persist_dir)

    def _get_client(self) -> ClientAPI:
        if self._client is None:
            self._client = self._connect()
        return self._client

    async def _run(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except NotFoundError:
            raise
        except ChromaError as exc:
            raise VectorStoreError(str(exc)) from exc

    def _collection(self, name: str) -> Any:
        # embedding_function=None throughout: vectors are supplied by the
        # configured provider. Chroma's default would otherwise download an
        # ONNX model and silently embed with it.
        return self._get_client().get_collection(name=name, embedding_function=None)

    # ----------------------------------------------------------- lifecycle ---
    async def ensure_collection(self, name: str, *, embedding_model: str, dimensions: int) -> None:
        def create() -> dict[str, Any] | None:
            collection = self._get_client().get_or_create_collection(
                name=name,
                configuration={"hnsw": {"space": self._distance}},
                metadata={_META_MODEL: embedding_model, _META_DIMENSIONS: dimensions},
                embedding_function=None,
            )
            return dict(collection.metadata or {})

        async with self._lock:
            metadata = await self._run(create)

        # An existing collection keeps the metadata it was created with, so
        # this reads back what is actually there rather than what we asked for.
        existing_model = (metadata or {}).get(_META_MODEL)
        existing_dimensions = (metadata or {}).get(_META_DIMENSIONS)
        if existing_model is not None and existing_model != embedding_model:
            raise VectorCollectionMismatch(
                f"Vector collection {name!r} holds vectors from {existing_model!r}, but the "
                f"configured model is {embedding_model!r}. Distances between two embedding "
                "spaces are meaningless. Re-embed the collection, or point it at a new "
                "vector collection."
            )
        if existing_dimensions is not None and int(existing_dimensions) != dimensions:
            raise VectorCollectionMismatch(
                f"Vector collection {name!r} holds {existing_dimensions}-dimensional vectors; "
                f"the configured model produces {dimensions}."
            )

    async def delete_collection(self, name: str) -> None:
        def drop() -> None:
            try:
                self._get_client().delete_collection(name)
            except NotFoundError:
                # Deleting what is already gone is the desired end state, and
                # cleanup must be safe to retry.
                logger.debug("Vector collection %s was already absent", name)

        async with self._lock:
            await self._run(drop)

    # --------------------------------------------------------------- write ---
    async def upsert(self, name: str, records: list[VectorRecord]) -> None:
        if not records:
            return

        def write() -> None:
            self._collection(name).upsert(
                ids=[record.id for record in records],
                embeddings=[record.embedding for record in records],
                documents=[record.document for record in records],
                metadatas=[_clean(record.metadata) for record in records],
            )

        await self._run(write)

    async def delete_document(self, name: str, document_id: uuid.UUID) -> int:
        return await self._delete_where(name, {META_DOCUMENT_ID: str(document_id)})

    async def delete_stale_chunks(
        self, name: str, document_id: uuid.UUID, *, keep_through_index: int
    ) -> int:
        return await self._delete_where(
            name,
            {
                "$and": [
                    {META_DOCUMENT_ID: {"$eq": str(document_id)}},
                    {META_CHUNK_INDEX: {"$gte": keep_through_index}},
                ]
            },
        )

    async def _delete_where(self, name: str, where: dict[str, Any]) -> int:
        def remove() -> int:
            try:
                collection = self._collection(name)
            except NotFoundError:
                return 0
            # Counted before deleting: Chroma's delete does not report how
            # many rows it matched, and the number is worth auditing.
            doomed = collection.get(where=where, include=[])["ids"]
            if doomed:
                collection.delete(ids=doomed)
            return len(doomed)

        return await self._run(remove)

    async def delete_ids(self, name: str, ids: list[str]) -> int:
        if not ids:
            return 0

        def remove() -> int:
            try:
                collection = self._collection(name)
            except NotFoundError:
                return 0
            # Chroma ignores ids it does not hold, so this is idempotent: a
            # reconciliation pass that is interrupted and restarted removes
            # what is left without complaining about what it already removed.
            collection.delete(ids=ids)
            return len(ids)

        return await self._run(remove)

    async def list_ids(self, name: str, *, limit: int, offset: int) -> list[str]:
        def page() -> list[str]:
            try:
                collection = self._collection(name)
            except NotFoundError:
                return []
            result = collection.get(limit=limit, offset=offset, include=[])
            return list(result.get("ids") or [])

        return await self._run(page)

    # ---------------------------------------------------------------- read ---
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
        # The tenant predicate is applied inside the store, never by filtering
        # results afterwards. A post-filter would still have *fetched* another
        # tenant's text into this process, and would silently return fewer
        # than top_k rows.
        clauses: list[dict[str, Any]] = [{META_NAMESPACE_ID: {"$eq": str(namespace_id)}}]
        if collection_id is not None:
            clauses.append({META_COLLECTION_ID: {"$eq": str(collection_id)}})
        if document_ids:
            clauses.append({META_DOCUMENT_ID: {"$in": [str(d) for d in document_ids]}})
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}

        def search() -> Any:
            try:
                return self._collection(name).query(
                    query_embeddings=[embedding],
                    n_results=top_k,
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
            except NotFoundError:
                # Nothing has been indexed into this collection yet. An empty
                # result is the honest answer; an error would make a brand-new
                # collection look broken.
                return None

        result = await self._run(search)
        if result is None or not result.get("ids") or not result["ids"][0]:
            return []

        ids = result["ids"][0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        return [
            VectorMatch(
                id=ids[i],
                score=_similarity(distances[i] if i < len(distances) else None, self._distance),
                document=documents[i] if i < len(documents) else "",
                metadata=dict(metadatas[i] or {}) if i < len(metadatas) else {},
            )
            for i in range(len(ids))
        ]

    async def count(self, name: str) -> int:
        def total() -> int:
            try:
                return self._collection(name).count()
            except NotFoundError:
                return 0

        return await self._run(total)

    async def heartbeat(self) -> bool:
        """Reach the store without touching anyone's data.

        For the persistent client this is cheap but not free: the first call
        opens the SQLite file, which is precisely what readiness should be
        checking - a directory that is missing, unwritable or on a volume
        that failed to mount fails here rather than on a user's first upload.

        Broad ``except``: a readiness probe that raises is a 500 where the
        honest answer is "not ready". Chroma also raises plain ``OSError``
        for a bad persist directory, which is not a ``ChromaError``.
        """

        def ping() -> bool:
            self._get_client().heartbeat()
            return True

        try:
            return bool(await asyncio.to_thread(ping))
        except Exception:
            logger.warning("Chroma heartbeat failed", exc_info=True)
            # A failed connection attempt must not be cached, or the store
            # stays "down" for the life of the process after one blip.
            self._client = None
            return False


def _clean(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop keys Chroma cannot store.

    Chroma accepts only scalars as metadata values, and rejects None outright
    - so an absent page number has to be omitted, not stored as null.
    """
    return {
        key: value
        for key, value in metadata.items()
        if value is not None and isinstance(value, str | int | float | bool)
    }


def _similarity(distance: float | None, space: str) -> float:
    """Convert Chroma's distance into a score where larger is better.

    Callers threshold on this, and a threshold that silently means the
    opposite of what it reads is a bug waiting to happen.
    """
    if distance is None:
        return 0.0
    if space == "cosine":
        # Chroma's cosine distance is 1 - cosine_similarity, in [0, 2].
        return 1.0 - float(distance)
    if space == "ip":
        return -float(distance)
    return 1.0 / (1.0 + float(distance))
