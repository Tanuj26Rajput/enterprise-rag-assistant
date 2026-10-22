"""Indexing and retrieval, from the service upwards and end to end.

Two layers:

* :class:`IndexingService` driven directly against a real Chroma, which is
  where the idempotence and cleanup guarantees live.
* The HTTP routes, which is where a cross-tenant leak would actually matter.
  Those go through the whole stack - upload, extract, chunk, embed, index,
  search - with only the SQL layer replaced, in keeping with the rest of the
  suite.
"""

from __future__ import annotations

import uuid
from hashlib import sha256
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.db.models import Collection, Document, DocumentChunk
from app.db.models.enums import DocumentStatus
from app.embeddings.hashing import HashingEmbeddingProvider
from app.embeddings.registry import resolve_spec
from app.services.indexing import IndexingError, IndexingService
from app.vectorstore.base import vector_id_for
from app.vectorstore.chroma import ChromaVectorStore
from tests.conftest import make_collection, make_namespace, signed_in

HANDBOOK = (
    "Employees accrue twenty-five days of annual leave each calendar year. "
    "Leave must be requested at least two weeks in advance through the HR "
    "portal, and unused days do not carry over past the thirty-first of March. "
)
SECURITY = (
    "All production access requires hardware multi-factor authentication. "
    "Credentials are rotated every ninety days, and shared accounts are "
    "prohibited without a documented exception approved by the security team. "
)


# --------------------------------------------------------------- service ---
def make_collection_row(
    *, namespace_id: uuid.UUID, spec: Any, vector_collection: str = "svc-test-vectors"
) -> Collection:
    return Collection(
        id=uuid.uuid4(),
        namespace_id=namespace_id,
        slug="policies",
        name="Policies",
        vector_collection=vector_collection,
        embedding_model=spec.model,
        embedding_dimensions=spec.dimensions,
    )


def make_document_row(collection: Collection) -> Document:
    return Document(
        id=uuid.uuid4(),
        namespace_id=collection.namespace_id,
        collection_id=collection.id,
        filename="handbook.txt",
        title="Handbook",
        mime_type="text/plain",
        size_bytes=len(HANDBOOK),
        checksum=sha256(HANDBOOK.encode()).hexdigest(),
        storage_uri="memory://handbook.txt",
        status=DocumentStatus.PROCESSING,
    )


def make_chunk_rows(document: Document, texts: list[str]) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            id=uuid.uuid4(),
            document_id=document.id,
            chunk_index=index,
            content=text,
            content_hash=sha256(text.encode()).hexdigest(),
            token_count=max(1, len(text.split())),
            page_number=index + 1,
        )
        for index, text in enumerate(texts)
    ]


@pytest.fixture
def indexing(
    vector_store: ChromaVectorStore,
    embeddings: HashingEmbeddingProvider,
    settings: Settings,
) -> IndexingService:
    return IndexingService(vector_store, embeddings, settings)


@pytest.fixture
def indexed_collection(embeddings: HashingEmbeddingProvider) -> Collection:
    return make_collection_row(namespace_id=uuid.uuid4(), spec=embeddings.spec)


class TestIndexDocument:
    async def test_it_writes_one_vector_per_chunk(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        document = make_document_row(indexed_collection)
        chunks = make_chunk_rows(document, ["alpha text", "beta text", "gamma text"])

        written = await indexing.index_document(document, indexed_collection, chunks)

        assert written == 3
        assert await vector_store.count(indexed_collection.vector_collection) == 3

    async def test_it_records_the_vector_id_on_each_chunk(
        self, indexing: IndexingService, indexed_collection: Collection
    ) -> None:
        """The join key back from a retrieval hit to the database row."""
        document = make_document_row(indexed_collection)
        chunks = make_chunk_rows(document, ["alpha text", "beta text"])

        await indexing.index_document(document, indexed_collection, chunks)

        assert [chunk.vector_id for chunk in chunks] == [
            vector_id_for(document.id, 0),
            vector_id_for(document.id, 1),
        ]
        assert all(chunk.embedded_at is not None for chunk in chunks)

    async def test_reindexing_is_idempotent(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        """A retry after a timeout must converge, not accumulate."""
        document = make_document_row(indexed_collection)
        chunks = make_chunk_rows(document, ["alpha text", "beta text", "gamma text"])

        for _ in range(3):
            await indexing.index_document(document, indexed_collection, chunks)

        assert await vector_store.count(indexed_collection.vector_collection) == 3

    async def test_a_shorter_reprocess_prunes_the_tail(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        """Vectors for chunks the document no longer has must not survive."""
        document = make_document_row(indexed_collection)
        long_version = make_chunk_rows(document, [f"chunk {n}" for n in range(6)])
        await indexing.index_document(document, indexed_collection, long_version)

        short_version = make_chunk_rows(document, ["chunk 0", "chunk 1"])
        await indexing.index_document(document, indexed_collection, short_version)

        assert await vector_store.count(indexed_collection.vector_collection) == 2

    async def test_a_document_that_now_yields_nothing_is_cleared(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        document = make_document_row(indexed_collection)
        await indexing.index_document(
            document, indexed_collection, make_chunk_rows(document, ["a", "b"])
        )

        written = await indexing.index_document(document, indexed_collection, [])

        assert written == 0
        assert await vector_store.count(indexed_collection.vector_collection) == 0

    async def test_a_second_document_is_unaffected_by_the_first_shrinking(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        first = make_document_row(indexed_collection)
        second = make_document_row(indexed_collection)
        await indexing.index_document(
            first, indexed_collection, make_chunk_rows(first, [f"a{n}" for n in range(4)])
        )
        await indexing.index_document(
            second, indexed_collection, make_chunk_rows(second, [f"b{n}" for n in range(4)])
        )

        await indexing.index_document(first, indexed_collection, make_chunk_rows(first, ["a0"]))

        assert await vector_store.count(indexed_collection.vector_collection) == 5


class TestEmbeddingCompatibility:
    async def test_a_collection_built_with_another_model_is_refused(
        self, indexing: IndexingService, embeddings: HashingEmbeddingProvider
    ) -> None:
        """Querying one embedding space with another returns confident nonsense."""
        collection = make_collection_row(
            namespace_id=uuid.uuid4(), spec=resolve_spec("openai", "text-embedding-3-small")
        )
        document = make_document_row(collection)

        with pytest.raises(IndexingError, match="Re-embed"):
            await indexing.index_document(document, collection, make_chunk_rows(document, ["text"]))

    async def test_a_collection_with_another_width_is_refused(
        self, indexing: IndexingService, embeddings: HashingEmbeddingProvider
    ) -> None:
        collection = make_collection_row(namespace_id=uuid.uuid4(), spec=embeddings.spec)
        collection.embedding_dimensions = 1536
        document = make_document_row(collection)

        with pytest.raises(IndexingError, match="dimensional"):
            await indexing.index_document(document, collection, make_chunk_rows(document, ["text"]))

    async def test_an_incompatible_collection_cannot_even_be_searched(
        self, indexing: IndexingService
    ) -> None:
        collection = make_collection_row(
            namespace_id=uuid.uuid4(), spec=resolve_spec("openai", "text-embedding-3-small")
        )

        with pytest.raises(IndexingError, match="Re-embed"):
            await indexing.search(
                query="anything",
                collection=collection,
                namespace_id=collection.namespace_id,
                top_k=5,
            )


class TestServiceSearch:
    @pytest.fixture
    async def populated(
        self, indexing: IndexingService, indexed_collection: Collection
    ) -> Collection:
        document = make_document_row(indexed_collection)
        await indexing.index_document(
            document,
            indexed_collection,
            make_chunk_rows(document, [HANDBOOK, SECURITY]),
        )
        return indexed_collection

    async def test_the_nearest_chunk_comes_first(
        self, indexing: IndexingService, populated: Collection
    ) -> None:
        matches = await indexing.search(
            query=SECURITY,
            collection=populated,
            namespace_id=populated.namespace_id,
            top_k=5,
        )

        assert matches[0].document == SECURITY

    async def test_min_score_drops_weak_hits(
        self, indexing: IndexingService, populated: Collection
    ) -> None:
        unfiltered = await indexing.search(
            query=SECURITY,
            collection=populated,
            namespace_id=populated.namespace_id,
            top_k=5,
        )
        filtered = await indexing.search(
            query=SECURITY,
            collection=populated,
            namespace_id=populated.namespace_id,
            top_k=5,
            min_score=0.999,
        )

        assert len(unfiltered) == 2
        assert len(filtered) == 1

    async def test_another_namespace_gets_nothing(
        self, indexing: IndexingService, populated: Collection
    ) -> None:
        """Even holding the right collection, the tenant predicate applies."""
        matches = await indexing.search(
            query=SECURITY,
            collection=populated,
            namespace_id=uuid.uuid4(),
            top_k=5,
        )

        assert matches == []


class TestServiceCleanup:
    async def test_removing_a_document_is_safe_to_repeat(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        document = make_document_row(indexed_collection)
        await indexing.index_document(
            document, indexed_collection, make_chunk_rows(document, ["a", "b"])
        )

        assert await indexing.remove_document(indexed_collection, document.id) == 2
        assert await indexing.remove_document(indexed_collection, document.id) == 0

    async def test_removing_a_collection_is_safe_to_repeat(
        self,
        indexing: IndexingService,
        vector_store: ChromaVectorStore,
        indexed_collection: Collection,
    ) -> None:
        document = make_document_row(indexed_collection)
        await indexing.index_document(
            document, indexed_collection, make_chunk_rows(document, ["a", "b"])
        )

        await indexing.remove_collection(indexed_collection)
        await indexing.remove_collection(indexed_collection)

        assert await vector_store.count(indexed_collection.vector_collection) == 0


# ------------------------------------------------------------ end to end ---
async def upload_text(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    collection_id: str,
    *,
    filename: str,
    body: str,
    **data: Any,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/namespaces/{namespace_id}/collections/{collection_id}/documents",
        headers=headers,
        files={"file": (filename, body.encode(), "text/plain")},
        data=data,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def search(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    collection_id: str,
    query: str,
    **body: Any,
) -> Any:
    return await client.post(
        f"/api/v1/namespaces/{namespace_id}/collections/{collection_id}/search",
        headers=headers,
        json={"query": query, **body},
    )


@pytest.fixture
async def owner(client: AsyncClient) -> dict[str, str]:
    return await signed_in(client, "owner@example.com")


@pytest.fixture
async def workspace(client: AsyncClient, owner: dict[str, str]) -> dict[str, str]:
    """A namespace with one collection holding two uploaded documents."""
    namespace = await make_namespace(client, owner, "acme")
    collection = await make_collection(client, owner, namespace["id"], "policies")
    handbook = await upload_text(
        client,
        owner,
        namespace["id"],
        collection["id"],
        filename="handbook.txt",
        body=HANDBOOK,
        title="Employee Handbook",
    )
    security = await upload_text(
        client,
        owner,
        namespace["id"],
        collection["id"],
        filename="security.txt",
        body=SECURITY,
    )
    return {
        "namespace_id": namespace["id"],
        "collection_id": collection["id"],
        "handbook_id": handbook["id"],
        "security_id": security["id"],
    }


class TestSearchEndpoint:
    async def test_upload_makes_a_document_searchable(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            "how many days of annual leave do employees get",
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["hits"]
        assert body["hits"][0]["document_id"] == workspace["handbook_id"]

    async def test_a_hit_carries_what_a_citation_needs(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            "multi-factor authentication for production access",
        )

        hit = response.json()["hits"][0]
        assert hit["document_title"] == "security.txt"  # falls back to the filename
        assert hit["chunk_index"] >= 0
        assert hit["content"]
        assert hit["score"] <= 1.0
        assert uuid.UUID(hit["chunk_id"])

    async def test_the_document_title_is_preferred_over_the_filename(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            HANDBOOK,
        )

        assert response.json()["hits"][0]["document_title"] == "Employee Handbook"

    async def test_top_k_bounds_the_response(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            "policy",
            top_k=1,
        )

        assert len(response.json()["hits"]) == 1

    async def test_an_empty_collection_returns_no_hits_not_an_error(
        self, client: AsyncClient, owner: dict[str, str]
    ) -> None:
        namespace = await make_namespace(client, owner, "empty-co")
        collection = await make_collection(client, owner, namespace["id"], "untouched")

        response = await search(client, owner, namespace["id"], collection["id"], "anything at all")

        assert response.status_code == 200
        assert response.json()["hits"] == []

    async def test_a_blank_query_is_rejected(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client, owner, workspace["namespace_id"], workspace["collection_id"], "   "
        )

        assert response.status_code == 422

    async def test_an_over_long_query_is_rejected(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            "x" * 5_000,
        )

        assert response.status_code == 422

    async def test_an_unknown_collection_is_a_404(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await search(
            client, owner, workspace["namespace_id"], str(uuid.uuid4()), "anything"
        )

        assert response.status_code == 404


class TestSearchIsolation:
    async def test_an_anonymous_request_is_rejected(
        self, client: AsyncClient, workspace: dict[str, str]
    ) -> None:
        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/collections/{workspace['collection_id']}/search",
            json={"query": "annual leave"},
        )

        assert response.status_code == 401

    async def test_a_non_member_cannot_search_another_tenant(
        self, client: AsyncClient, workspace: dict[str, str]
    ) -> None:
        """404, not 403: membership of a namespace is not enumerable."""
        outsider = await signed_in(client, "outsider@example.com")

        response = await search(
            client,
            outsider,
            workspace["namespace_id"],
            workspace["collection_id"],
            "annual leave entitlement",
        )

        assert response.status_code == 404

    async def test_one_tenants_documents_never_appear_in_anothers_results(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        """The leak this whole design exists to prevent."""
        rival = await signed_in(client, "rival@example.com")
        rival_namespace = await make_namespace(client, rival, "globex")
        rival_collection = await make_collection(client, rival, rival_namespace["id"], "policies")
        await upload_text(
            client,
            rival,
            rival_namespace["id"],
            rival_collection["id"],
            filename="theirs.txt",
            body="Globex staff may claim mileage at forty pence per mile.",
        )

        response = await search(
            client,
            rival,
            rival_namespace["id"],
            rival_collection["id"],
            # Word for word out of the other tenant's handbook.
            HANDBOOK,
        )

        assert response.status_code == 200
        hits = response.json()["hits"]
        assert all(hit["document_id"] != workspace["handbook_id"] for hit in hits)
        assert all(HANDBOOK not in hit["content"] for hit in hits)


class TestDeletionRemovesVectors:
    async def test_a_deleted_document_stops_being_retrievable(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        deleted = await client.delete(
            f"/api/v1/namespaces/{workspace['namespace_id']}/documents/{workspace['handbook_id']}",
            headers=owner,
        )
        assert deleted.status_code == 204

        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            HANDBOOK,
        )

        hits = response.json()["hits"]
        assert all(hit["document_id"] != workspace["handbook_id"] for hit in hits)

    async def test_deleting_one_document_leaves_the_rest_searchable(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        await client.delete(
            f"/api/v1/namespaces/{workspace['namespace_id']}/documents/{workspace['handbook_id']}",
            headers=owner,
        )

        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            SECURITY,
        )

        hits = response.json()["hits"]
        assert hits
        assert hits[0]["document_id"] == workspace["security_id"]

    async def test_a_deleted_collection_takes_its_index_with_it(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        vector_store: ChromaVectorStore,
        store: Any,
    ) -> None:
        collection = store.collections[uuid.UUID(workspace["collection_id"])]
        name = collection.vector_collection
        assert await vector_store.count(name) > 0

        response = await client.delete(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/collections/{workspace['collection_id']}",
            headers=owner,
        )

        assert response.status_code == 204
        assert await vector_store.count(name) == 0


class TestReprocessing:
    async def test_reprocessing_does_not_duplicate_vectors(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        vector_store: ChromaVectorStore,
        store: Any,
    ) -> None:
        name = store.collections[uuid.UUID(workspace["collection_id"])].vector_collection
        before = await vector_store.count(name)

        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/documents/{workspace['handbook_id']}/reprocess",
            headers=owner,
        )

        assert response.status_code == 200
        assert await vector_store.count(name) == before

    async def test_reprocessing_leaves_the_document_searchable(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/documents/{workspace['handbook_id']}/reprocess",
            headers=owner,
        )

        response = await search(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            HANDBOOK,
        )

        assert response.json()["hits"][0]["document_id"] == workspace["handbook_id"]
