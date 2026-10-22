"""Integration tests against a real ChromaDB.

These run against an actual persistent Chroma on a per-test directory rather
than a double. The guarantees asserted here - idempotent upsert, tenant
filtering inside the query, tail pruning, deletion cleanup, durability across
client instances - are all properties of Chroma's own behaviour, so a fake
would only assert my beliefs about Chroma back at me.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.embeddings.hashing import HashingEmbeddingProvider
from app.vectorstore.base import (
    META_CHUNK_INDEX,
    META_COLLECTION_ID,
    META_DOCUMENT_ID,
    META_NAMESPACE_ID,
    META_PAGE_NUMBER,
    VectorCollectionMismatch,
    VectorRecord,
    VectorStoreError,
    vector_id_for,
)
from app.vectorstore.chroma import ChromaVectorStore

COLLECTION = "test-vectors"


def record(
    *,
    document_id: uuid.UUID,
    chunk_index: int,
    embedding: list[float],
    text: str,
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    page_number: int | None = None,
) -> VectorRecord:
    return VectorRecord(
        id=vector_id_for(document_id, chunk_index),
        embedding=embedding,
        document=text,
        metadata={
            META_NAMESPACE_ID: str(namespace_id),
            META_COLLECTION_ID: str(collection_id),
            META_DOCUMENT_ID: str(document_id),
            META_CHUNK_INDEX: chunk_index,
            META_PAGE_NUMBER: page_number,
        },
    )


@pytest.fixture
async def ready(
    vector_store: ChromaVectorStore, embeddings: HashingEmbeddingProvider
) -> ChromaVectorStore:
    await vector_store.ensure_collection(
        COLLECTION,
        embedding_model=embeddings.spec.model,
        dimensions=embeddings.spec.dimensions,
    )
    return vector_store


class TestVectorIds:
    def test_an_id_is_derived_not_generated(self) -> None:
        document_id = uuid.uuid4()

        assert vector_id_for(document_id, 3) == vector_id_for(document_id, 3)
        assert vector_id_for(document_id, 3) == f"{document_id}:3"

    def test_different_chunks_get_different_ids(self) -> None:
        document_id = uuid.uuid4()

        assert vector_id_for(document_id, 0) != vector_id_for(document_id, 1)

    def test_a_negative_index_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            vector_id_for(uuid.uuid4(), -1)


class TestCollectionLifecycle:
    async def test_ensure_is_idempotent(self, vector_store: ChromaVectorStore) -> None:
        for _ in range(3):
            await vector_store.ensure_collection(
                COLLECTION, embedding_model="hash-256", dimensions=256
            )

        assert await vector_store.count(COLLECTION) == 0

    async def test_a_different_model_is_refused(self, ready: ChromaVectorStore) -> None:
        """Two embedding spaces in one index makes every distance a lie."""
        with pytest.raises(VectorCollectionMismatch, match="meaningless"):
            await ready.ensure_collection(
                COLLECTION, embedding_model="text-embedding-3-small", dimensions=256
            )

    async def test_a_different_width_is_refused(self, ready: ChromaVectorStore) -> None:
        with pytest.raises(VectorCollectionMismatch, match="dimensional"):
            await ready.ensure_collection(COLLECTION, embedding_model="hash-256", dimensions=1536)

    async def test_counting_an_absent_collection_is_zero_not_an_error(
        self, vector_store: ChromaVectorStore
    ) -> None:
        assert await vector_store.count("never-created-collection") == 0

    async def test_deleting_an_absent_collection_succeeds(
        self, vector_store: ChromaVectorStore
    ) -> None:
        """Cleanup has to be safe to retry."""
        await vector_store.delete_collection("never-created-collection")

    async def test_deleting_twice_succeeds(self, ready: ChromaVectorStore) -> None:
        await ready.delete_collection(COLLECTION)
        await ready.delete_collection(COLLECTION)

        assert await ready.count(COLLECTION) == 0


class TestUpsert:
    async def test_records_round_trip_with_their_metadata(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))
        vectors = await embeddings.embed_documents(["the first chunk"])

        await ready.upsert(
            COLLECTION,
            [
                record(
                    document_id=document_id,
                    chunk_index=0,
                    embedding=vectors[0],
                    text="the first chunk",
                    namespace_id=namespace_id,
                    collection_id=collection_id,
                    page_number=7,
                )
            ],
        )

        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("the first chunk"),
            namespace_id=namespace_id,
            top_k=5,
        )

        assert len(hits) == 1
        assert hits[0].document == "the first chunk"
        assert hits[0].document_id == document_id
        assert hits[0].chunk_index == 0
        assert hits[0].metadata[META_PAGE_NUMBER] == 7
        assert hits[0].metadata[META_COLLECTION_ID] == str(collection_id)
        assert hits[0].metadata[META_DOCUMENT_ID] == str(document_id)

    async def test_a_null_page_number_is_omitted_not_stored(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        """Chroma rejects None outright, so it has to be dropped on write."""
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))
        vector = await embeddings.embed_query("a docx chunk has no page")

        await ready.upsert(
            COLLECTION,
            [
                record(
                    document_id=document_id,
                    chunk_index=0,
                    embedding=vector,
                    text="a docx chunk has no page",
                    namespace_id=namespace_id,
                    collection_id=collection_id,
                    page_number=None,
                )
            ],
        )

        hits = await ready.query(COLLECTION, embedding=vector, namespace_id=namespace_id, top_k=5)

        assert len(hits) == 1
        assert META_PAGE_NUMBER not in hits[0].metadata

    async def test_reindexing_overwrites_instead_of_duplicating(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        """The retry case: the same work done three times converges."""
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))
        texts = ["clause one", "clause two", "clause three"]
        vectors = await embeddings.embed_documents(texts)
        records = [
            record(
                document_id=document_id,
                chunk_index=index,
                embedding=vector,
                text=text,
                namespace_id=namespace_id,
                collection_id=collection_id,
            )
            for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ]

        for _ in range(3):
            await ready.upsert(COLLECTION, records)

        assert await ready.count(COLLECTION) == 3

    async def test_an_edited_chunk_replaces_the_old_text(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))

        async def write(text: str) -> None:
            await ready.upsert(
                COLLECTION,
                [
                    record(
                        document_id=document_id,
                        chunk_index=0,
                        embedding=await embeddings.embed_query(text),
                        text=text,
                        namespace_id=namespace_id,
                        collection_id=collection_id,
                    )
                ],
            )

        await write("the old wording")
        await write("the new wording")

        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("the new wording"),
            namespace_id=namespace_id,
            top_k=5,
        )

        assert [hit.document for hit in hits] == ["the new wording"]

    async def test_an_empty_write_is_a_no_op(self, ready: ChromaVectorStore) -> None:
        await ready.upsert(COLLECTION, [])

        assert await ready.count(COLLECTION) == 0


class TestTenantFiltering:
    @pytest.fixture
    async def two_tenants(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> dict[str, uuid.UUID]:
        """Identical text indexed under two namespaces, one collection each."""
        ids = {
            key: uuid.uuid4()
            for key in (
                "acme_ns",
                "acme_col",
                "acme_doc",
                "globex_ns",
                "globex_col",
                "globex_doc",
            )
        }
        text = "the quarterly revenue figure"
        vector = await embeddings.embed_query(text)

        await ready.upsert(
            COLLECTION,
            [
                record(
                    document_id=ids["acme_doc"],
                    chunk_index=0,
                    embedding=vector,
                    text=f"acme: {text}",
                    namespace_id=ids["acme_ns"],
                    collection_id=ids["acme_col"],
                ),
                record(
                    document_id=ids["globex_doc"],
                    chunk_index=0,
                    embedding=vector,
                    text=f"globex: {text}",
                    namespace_id=ids["globex_ns"],
                    collection_id=ids["globex_col"],
                ),
            ],
        )
        return ids

    async def test_a_query_sees_only_its_own_namespace(
        self,
        ready: ChromaVectorStore,
        embeddings: HashingEmbeddingProvider,
        two_tenants: dict[str, uuid.UUID],
    ) -> None:
        """Both rows are an exact match; only one may come back."""
        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("the quarterly revenue figure"),
            namespace_id=two_tenants["acme_ns"],
            top_k=10,
        )

        assert len(hits) == 1
        assert hits[0].document.startswith("acme:")

    async def test_an_unrelated_namespace_sees_nothing(
        self,
        ready: ChromaVectorStore,
        embeddings: HashingEmbeddingProvider,
        two_tenants: dict[str, uuid.UUID],
    ) -> None:
        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("the quarterly revenue figure"),
            namespace_id=uuid.uuid4(),
            top_k=10,
        )

        assert hits == []

    async def test_a_collection_filter_narrows_within_the_namespace(
        self,
        ready: ChromaVectorStore,
        embeddings: HashingEmbeddingProvider,
        two_tenants: dict[str, uuid.UUID],
    ) -> None:
        """Namespace and collection are ANDed, not ORed."""
        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("the quarterly revenue figure"),
            namespace_id=two_tenants["acme_ns"],
            collection_id=two_tenants["globex_col"],
            top_k=10,
        )

        assert hits == []

    async def test_a_document_filter_restricts_the_result(
        self,
        ready: ChromaVectorStore,
        embeddings: HashingEmbeddingProvider,
        two_tenants: dict[str, uuid.UUID],
    ) -> None:
        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("the quarterly revenue figure"),
            namespace_id=two_tenants["acme_ns"],
            document_ids=[two_tenants["acme_doc"]],
            top_k=10,
        )

        assert [hit.document_id for hit in hits] == [two_tenants["acme_doc"]]

    async def test_querying_a_collection_that_was_never_created_is_empty(
        self, vector_store: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        """A collection with nothing indexed must look empty, not broken."""
        hits = await vector_store.query(
            "not-yet-indexed",
            embedding=await embeddings.embed_query("anything"),
            namespace_id=uuid.uuid4(),
            top_k=5,
        )

        assert hits == []


class TestRanking:
    async def test_a_closer_chunk_scores_higher(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        """Larger score means nearer; a threshold must not read backwards."""
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))
        texts = [
            "parental leave entitlement and pay",
            "server rack decommissioning procedure",
        ]
        vectors = await embeddings.embed_documents(texts)
        await ready.upsert(
            COLLECTION,
            [
                record(
                    document_id=document_id,
                    chunk_index=index,
                    embedding=vector,
                    text=text,
                    namespace_id=namespace_id,
                    collection_id=collection_id,
                )
                for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
            ],
        )

        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query(texts[0]),
            namespace_id=namespace_id,
            top_k=2,
        )

        assert hits[0].document == texts[0]
        assert hits[0].score > hits[1].score
        # An exact match under cosine is distance 0, so score 1.
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)

    async def test_top_k_bounds_the_result(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> None:
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))
        texts = [f"paragraph number {n}" for n in range(10)]
        vectors = await embeddings.embed_documents(texts)
        await ready.upsert(
            COLLECTION,
            [
                record(
                    document_id=document_id,
                    chunk_index=index,
                    embedding=vector,
                    text=text,
                    namespace_id=namespace_id,
                    collection_id=collection_id,
                )
                for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
            ],
        )

        hits = await ready.query(
            COLLECTION,
            embedding=await embeddings.embed_query("paragraph number 3"),
            namespace_id=namespace_id,
            top_k=3,
        )

        assert len(hits) == 3


class TestDeletion:
    @pytest.fixture
    async def indexed(
        self, ready: ChromaVectorStore, embeddings: HashingEmbeddingProvider
    ) -> dict[str, uuid.UUID]:
        """Two documents, five chunks each, in one namespace."""
        ids = {
            "namespace": uuid.uuid4(),
            "collection": uuid.uuid4(),
            "doc_a": uuid.uuid4(),
            "doc_b": uuid.uuid4(),
        }
        for key in ("doc_a", "doc_b"):
            texts = [f"{key} chunk {n}" for n in range(5)]
            vectors = await embeddings.embed_documents(texts)
            await ready.upsert(
                COLLECTION,
                [
                    record(
                        document_id=ids[key],
                        chunk_index=index,
                        embedding=vector,
                        text=text,
                        namespace_id=ids["namespace"],
                        collection_id=ids["collection"],
                    )
                    for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
                ],
            )
        return ids

    async def test_deleting_a_document_leaves_the_others(
        self, ready: ChromaVectorStore, indexed: dict[str, uuid.UUID]
    ) -> None:
        removed = await ready.delete_document(COLLECTION, indexed["doc_a"])

        assert removed == 5
        assert await ready.count(COLLECTION) == 5

    async def test_deleting_a_document_twice_removes_nothing_the_second_time(
        self, ready: ChromaVectorStore, indexed: dict[str, uuid.UUID]
    ) -> None:
        await ready.delete_document(COLLECTION, indexed["doc_a"])

        assert await ready.delete_document(COLLECTION, indexed["doc_a"]) == 0

    async def test_deleting_from_an_absent_collection_is_zero(
        self, vector_store: ChromaVectorStore
    ) -> None:
        assert await vector_store.delete_document("no-such-collection", uuid.uuid4()) == 0

    async def test_a_shrunken_document_loses_its_tail(
        self, ready: ChromaVectorStore, indexed: dict[str, uuid.UUID]
    ) -> None:
        """Re-processing into fewer chunks must not leave the surplus behind."""
        pruned = await ready.delete_stale_chunks(COLLECTION, indexed["doc_a"], keep_through_index=2)

        assert pruned == 3
        assert await ready.count(COLLECTION) == 7

    async def test_pruning_does_not_touch_another_document(
        self, ready: ChromaVectorStore, indexed: dict[str, uuid.UUID]
    ) -> None:
        await ready.delete_stale_chunks(COLLECTION, indexed["doc_a"], keep_through_index=0)

        assert await ready.delete_document(COLLECTION, indexed["doc_b"]) == 5

    async def test_pruning_a_document_that_did_not_shrink_removes_nothing(
        self, ready: ChromaVectorStore, indexed: dict[str, uuid.UUID]
    ) -> None:
        pruned = await ready.delete_stale_chunks(COLLECTION, indexed["doc_a"], keep_through_index=5)

        assert pruned == 0
        assert await ready.count(COLLECTION) == 10

    async def test_dropping_the_collection_removes_everything(
        self, ready: ChromaVectorStore, indexed: dict[str, uuid.UUID]
    ) -> None:
        await ready.delete_collection(COLLECTION)

        assert await ready.count(COLLECTION) == 0


class TestPersistence:
    async def test_vectors_survive_a_new_client(
        self,
        settings: Settings,
        ready: ChromaVectorStore,
        embeddings: HashingEmbeddingProvider,
    ) -> None:
        """Persistent, not in-memory: a restart must not lose the index."""
        namespace_id, collection_id, document_id = (uuid.uuid4() for _ in range(3))
        vector = await embeddings.embed_query("durable text")
        await ready.upsert(
            COLLECTION,
            [
                record(
                    document_id=document_id,
                    chunk_index=0,
                    embedding=vector,
                    text="durable text",
                    namespace_id=namespace_id,
                    collection_id=collection_id,
                )
            ],
        )

        # A second store object over the same directory stands in for a
        # process restart.
        reopened = ChromaVectorStore(
            persist_dir=settings.chroma_persist_dir, distance=settings.chroma_distance
        )
        hits = await reopened.query(
            COLLECTION, embedding=vector, namespace_id=namespace_id, top_k=5
        )

        assert [hit.document for hit in hits] == ["durable text"]

    async def test_the_persist_directory_is_actually_written(
        self, settings: Settings, ready: ChromaVectorStore
    ) -> None:
        assert Path(settings.chroma_persist_dir).exists()


class TestConfiguration:
    async def test_a_store_with_no_destination_is_refused(self) -> None:
        """Neither a directory nor a host is a misconfiguration, not a default."""
        store = ChromaVectorStore(persist_dir=None, host=None)

        with pytest.raises(VectorStoreError, match="CHROMA_PERSIST_DIR"):
            await store.count("anything")
