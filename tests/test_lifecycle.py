"""Document lifecycle: retries, failed ingestion, deletion and stale vectors.

These tests are about what happens when something goes wrong, so almost all
of them start by *making* something go wrong. Two techniques appear
throughout:

* A scripted processor that fails a given number of times. It stands in for
  the ingestion pipeline so a test can describe a failure pattern - two
  timeouts then success - without needing a document that actually behaves
  that way.
* Monkeypatching extraction or the vector store, for the paths that have to
  be exercised end to end through HTTP, where the point is what the API
  reports rather than what the service returns.

Chroma is real throughout, as everywhere else in this suite: orphaned vectors
and their removal are exactly the thing a fake store would get wrong.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.core.exceptions import ValidationFailed
from app.db.models import Document, DocumentStatus, IngestionJob, JobKind, JobStatus
from app.services.indexing import IndexingService
from app.services.lifecycle import DocumentLifecycleService, backoff_seconds
from app.vectorstore.base import VectorRecord, VectorStoreError
from tests.conftest import make_collection, make_namespace, signed_in
from tests.fakes import (
    FakeAuditRepository,
    FakeCollectionRepository,
    FakeDocumentChunkRepository,
    FakeDocumentRepository,
    FakeJobRepository,
    FakeStore,
)
from tests.test_indexing import HANDBOOK, SECURITY, upload_text


class ScriptedProcessor:
    """An ingestion pipeline that fails on command.

    ``failures`` is what each of the first N attempts raises; attempts past
    the end of the list succeed. That lets a test state "two timeouts then
    success" directly, instead of contriving a document that produces it.
    """

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def process(self, document: Document, *, reraise: bool = False) -> Document:
        self.calls += 1
        if self.calls <= len(self.failures):
            document.status = DocumentStatus.FAILED
            raise self.failures[self.calls - 1]
        document.status = DocumentStatus.READY
        document.chunk_count = 3
        return document


def make_document(store: FakeStore, **overrides: Any) -> Document:
    """A persisted document, without going through an upload."""
    document = Document(
        id=uuid.uuid4(),
        namespace_id=overrides.pop("namespace_id", uuid.uuid4()),
        collection_id=overrides.pop("collection_id", uuid.uuid4()),
        filename="handbook.txt",
        mime_type="text/plain",
        size_bytes=1024,
        checksum=uuid.uuid4().hex,
        storage_uri="file://handbook.txt",
        status=overrides.pop("status", DocumentStatus.PENDING),
        **overrides,
    )
    from tests.fakes import materialize

    materialize(document)
    store.documents[document.id] = document
    return document


@pytest.fixture
def lifecycle(
    store: FakeStore,
    settings: Settings,
    vector_store: Any,
    embeddings: Any,
) -> DocumentLifecycleService:
    """The lifecycle service over fakes, with no processor attached yet.

    The authorizer is None: every method exercised here is machinery, which
    by design has no actor to authorize. The request-facing methods are
    tested over HTTP instead, where the authorization is real.
    """
    return DocumentLifecycleService(
        FakeJobRepository(store),
        FakeDocumentRepository(store),
        FakeDocumentChunkRepository(store),
        FakeCollectionRepository(store),
        FakeAuditRepository(store),
        None,  # type: ignore[arg-type]
        IndexingService(vector_store, embeddings, settings),
        settings,
        processor=None,
    )


def with_processor(
    lifecycle: DocumentLifecycleService, processor: ScriptedProcessor
) -> ScriptedProcessor:
    lifecycle._processor = processor
    return processor


# --------------------------------------------------------------- enqueueing ---
class TestEnqueueIsIdempotent:
    async def test_enqueuing_twice_returns_the_same_job(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """A double-clicked button must not queue the work twice.

        Two jobs would race to rewrite the same chunk rows, and the second
        would undo whatever the first had just done.
        """
        document = make_document(store)

        first = await lifecycle.enqueue(document, JobKind.INGEST)
        second = await lifecycle.enqueue(document, JobKind.INGEST)

        assert first.id == second.id
        assert len(store.jobs) == 1

    async def test_a_finished_job_does_not_block_a_new_one(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Idempotence covers *outstanding* work, not the document's history."""
        document = make_document(store)
        first = await lifecycle.enqueue(document, JobKind.INGEST)
        first.status = JobStatus.SUCCEEDED

        second = await lifecycle.enqueue(document, JobKind.INGEST)

        assert second.id != first.id
        assert len(store.jobs) == 2

    async def test_different_kinds_are_tracked_separately(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        document = make_document(store)

        ingest = await lifecycle.enqueue(document, JobKind.INGEST)
        delete = await lifecycle.enqueue(document, JobKind.DELETE)

        assert ingest.id != delete.id

    async def test_queueing_moves_the_document_out_of_pending(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """`pending` with no job is a lost upload; `queued` is merely waiting."""
        document = make_document(store, status=DocumentStatus.FAILED)
        document.error_message = "an older failure"

        await lifecycle.enqueue(document, JobKind.INGEST)

        assert document.status is DocumentStatus.QUEUED
        assert document.error_message is None

    async def test_a_delete_job_does_not_touch_the_document_status(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Only the purge itself may move a deleting document to archived."""
        document = make_document(store, status=DocumentStatus.DELETING)

        await lifecycle.enqueue(document, JobKind.DELETE)

        assert document.status is DocumentStatus.DELETING


# ------------------------------------------------------------------ retries ---
class TestBoundedRetries:
    async def test_a_transient_failure_is_retried_and_then_succeeds(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        processor = with_processor(
            lifecycle, ScriptedProcessor(TimeoutError("the embedding provider timed out"))
        )
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        first = await lifecycle.run_now(job)
        assert first.will_retry
        assert first.attempts == 1
        assert job.status is JobStatus.QUEUED

        # The second attempt is what a worker would do once run_after passes.
        second = await lifecycle.run_now(job)
        assert second.succeeded
        assert second.attempts == 2
        assert processor.calls == 2
        assert document.status is DocumentStatus.READY

    async def test_a_document_awaiting_retry_is_queued_not_failed(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """An operator's "needs attention" list must not fill with scheduled work."""
        with_processor(lifecycle, ScriptedProcessor(TimeoutError("timed out")))
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        await lifecycle.run_now(job)

        assert document.status is DocumentStatus.QUEUED
        assert document.error_message == "timed out"

    async def test_attempts_are_bounded_and_the_job_dead_letters(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Three attempts, then stop - and leave a record of why."""
        failures = [TimeoutError("timed out") for _ in range(5)]
        processor = with_processor(lifecycle, ScriptedProcessor(*failures))
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        results = [await lifecycle.run_now(job) for _ in range(3)]

        assert [result.status for result in results] == [
            JobStatus.QUEUED,
            JobStatus.QUEUED,
            JobStatus.FAILED,
        ]
        assert processor.calls == 3
        assert job.attempts == job.max_attempts == 3
        assert job.last_error == "timed out"
        assert job.finished_at is not None
        assert document.status is DocumentStatus.FAILED

    async def test_the_retry_is_scheduled_rather_than_waited_for(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Backoff lives in `run_after`, so it survives a restart.

        Sleeping in the worker would lose the delay on every deploy, and hold
        a process open doing nothing.
        """
        with_processor(lifecycle, ScriptedProcessor(TimeoutError("timed out")))
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        before = datetime.now(UTC)
        await lifecycle.run_now(job)

        assert job.run_after > before + timedelta(seconds=1)

    async def test_backoff_grows_and_is_capped(self) -> None:
        assert backoff_seconds(1) == 5
        assert backoff_seconds(2) == 10
        assert backoff_seconds(3) == 20
        # Unbounded doubling would eventually schedule a retry years out.
        assert backoff_seconds(50) == 600

    async def test_a_lease_is_released_whichever_way_the_job_ends(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """A finished job must not look claimed, or recovery would skip it."""
        with_processor(lifecycle, ScriptedProcessor(TimeoutError("timed out")))
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)
        job.lease_owner = "worker-1"
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        await lifecycle.run_now(job)

        assert job.lease_owner is None
        assert job.lease_expires_at is None


class TestPermanentFailures:
    async def test_a_corrupt_file_is_not_retried(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """A password-protected PDF does not open on the third attempt.

        Retrying it would spend the whole budget - and the user's time - on a
        failure that is already final.
        """
        processor = with_processor(
            lifecycle, ScriptedProcessor(ValidationFailed("This PDF is password protected."))
        )
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        result = await lifecycle.run_now(job)

        assert result.status is JobStatus.FAILED
        assert result.attempts == 1
        assert processor.calls == 1
        assert job.attempts_remaining == 2  # budget left, deliberately unspent

    async def test_a_permanent_failure_explains_itself_to_the_user(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        with_processor(
            lifecycle, ScriptedProcessor(ValidationFailed("This PDF is password protected."))
        )
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        await lifecycle.run_now(job)

        assert document.status is DocumentStatus.FAILED
        assert document.error_message == "This PDF is password protected."

    async def test_the_two_kinds_of_failure_are_audited_differently(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """ "Will be retried" and "given up on" are different operational events."""
        with_processor(lifecycle, ScriptedProcessor(TimeoutError("timed out")))
        transient = make_document(store)
        await lifecycle.run_now(await lifecycle.enqueue(transient, JobKind.INGEST))

        with_processor(lifecycle, ScriptedProcessor(ValidationFailed("corrupt")))
        permanent = make_document(store)
        await lifecycle.run_now(await lifecycle.enqueue(permanent, JobKind.INGEST))

        retried = store.events("document.job.retry")
        dead = store.events("document.job.failed")
        assert [entry.resource_id for entry in retried] == [transient.id]
        assert [entry.resource_id for entry in dead] == [permanent.id]
        assert dead[0].meta["permanent"] is True
        assert retried[0].meta["permanent"] is False


class TestRunnerRobustness:
    async def test_a_failing_job_does_not_raise_at_the_worker(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """One unreadable file must not take down a worker loop."""
        with_processor(lifecycle, ScriptedProcessor(RuntimeError("boom")))
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        result = await lifecycle.run_now(job)

        assert result.error == "boom"

    async def test_a_job_for_a_vanished_document_completes_quietly(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Nothing to do and nothing to retry: looping on it would be a leak."""
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)
        del store.documents[document.id]

        result = await lifecycle.run_now(job)

        assert result.succeeded

    async def test_an_unnamed_exception_still_records_something_useful(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """`str(exc)` is empty for many exceptions; "failed: " helps nobody."""
        with_processor(lifecycle, ScriptedProcessor(RuntimeError()))
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        result = await lifecycle.run_now(job)

        assert result.error == "RuntimeError"


# ----------------------------------------------------------------- recovery ---
class TestFailureRecovery:
    async def test_an_expired_lease_returns_the_job_to_the_queue(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """A worker killed mid-job gives its work back by timing out.

        Nothing notices the death; the lease simply stops being believed.
        """
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)
        job.status = JobStatus.RUNNING
        job.attempts = 1
        job.lease_owner = "worker-that-died"
        job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)

        recovered = await lifecycle.recover_abandoned()

        assert recovered == 1
        assert job.status is JobStatus.QUEUED
        assert job.lease_owner is None

    async def test_a_live_lease_is_left_alone(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Recovery must not steal work from a healthy worker."""
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)
        job.status = JobStatus.RUNNING
        job.lease_owner = "worker-1"
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        assert await lifecycle.recover_abandoned() == 0
        assert job.status is JobStatus.RUNNING

    async def test_a_job_that_keeps_killing_its_worker_is_dead_lettered(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Recovery is bounded by the same attempt budget as failure.

        Otherwise a document that reliably crashes the process would be
        requeued forever, taking the worker down with it each time.
        """
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)
        job.status = JobStatus.RUNNING
        job.attempts = job.max_attempts
        job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)

        await lifecycle.recover_abandoned()

        assert job.status is JobStatus.FAILED
        assert "stopped without reporting" in (job.last_error or "")

    async def test_claim_and_run_takes_a_due_job(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        with_processor(lifecycle, ScriptedProcessor())
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)

        result = await lifecycle.claim_and_run(owner="worker-1")

        assert result is not None
        assert result.job_id == job.id
        assert result.succeeded

    async def test_claim_and_run_skips_a_job_that_is_not_due_yet(
        self, lifecycle: DocumentLifecycleService, store: FakeStore
    ) -> None:
        """Backoff is only real if the claim query honours it."""
        with_processor(lifecycle, ScriptedProcessor())
        document = make_document(store)
        job = await lifecycle.enqueue(document, JobKind.INGEST)
        job.run_after = datetime.now(UTC) + timedelta(minutes=5)

        assert await lifecycle.claim_and_run(owner="worker-1") is None

    async def test_an_idle_queue_returns_nothing(self, lifecycle: DocumentLifecycleService) -> None:
        assert await lifecycle.claim_and_run(owner="worker-1") is None


# ------------------------------------------------------- ingestion over HTTP ---
class TestFailedIngestionOverHttp:
    async def test_a_permanent_extraction_failure_is_reported_at_once(
        self, client: AsyncClient, store: FakeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upload response tells the truth about a file that cannot be read."""

        def explode(*_: Any, **__: Any) -> Any:
            raise ValidationFailed("This document is password protected.")

        monkeypatch.setattr("app.services.document.extract_text", explode)

        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        assert document["status"] == DocumentStatus.FAILED.value
        assert document["error_message"] == "This document is password protected."
        job = next(iter(store.jobs.values()))
        assert job.status is JobStatus.FAILED
        assert job.attempts == 1

    async def test_a_transient_failure_leaves_the_document_queued_for_retry(
        self, client: AsyncClient, store: FakeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not `failed`: the work is scheduled, and saying otherwise misleads."""

        def explode(*_: Any, **__: Any) -> Any:
            raise OSError("the storage backend is unreachable")

        monkeypatch.setattr("app.services.document.extract_text", explode)

        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        assert document["status"] == DocumentStatus.QUEUED.value
        job = next(iter(store.jobs.values()))
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 1

    async def test_the_job_history_is_visible_to_the_owner(self, client: AsyncClient) -> None:
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        response = await client.get(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}/jobs",
            headers=headers,
        )

        assert response.status_code == 200
        jobs = response.json()
        assert [job["kind"] for job in jobs] == [JobKind.INGEST.value]
        assert jobs[0]["status"] == JobStatus.SUCCEEDED.value

    async def test_the_job_history_never_names_the_worker_holding_the_lease(
        self, client: AsyncClient
    ) -> None:
        """`lease_owner` identifies an internal process; it is operator-facing."""
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        response = await client.get(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}/jobs",
            headers=headers,
        )

        assert "lease_owner" not in response.json()[0]

    async def test_another_tenant_cannot_read_a_documents_jobs(self, client: AsyncClient) -> None:
        owner = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, owner, "acme")
        collection = await make_collection(client, owner, namespace["id"], "handbook")
        document = await upload_text(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        outsider = await signed_in(client, "outsider@example.com")
        response = await client.get(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}/jobs",
            headers=outsider,
        )

        assert response.status_code == 404


class TestReprocessing:
    async def test_reprocessing_replaces_rather_than_duplicates(
        self, client: AsyncClient, vector_store: Any, store: FakeStore
    ) -> None:
        """Ingestion is idempotent, which is what makes retrying it safe.

        If re-running doubled the chunks, every transient failure would
        corrupt the document it was supposed to rescue.
        """
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        name = next(iter(store.collections.values())).vector_collection
        before = await vector_store.count(name)

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}/reprocess",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["chunk_count"] == document["chunk_count"]
        assert await vector_store.count(name) == before

    async def test_reprocessing_records_a_reindex_job(
        self, client: AsyncClient, store: FakeStore
    ) -> None:
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        await client.post(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}/reprocess",
            headers=headers,
        )

        kinds = sorted(job.kind.value for job in store.jobs.values())
        assert kinds == [JobKind.INGEST.value, JobKind.REINDEX.value]

    async def test_a_viewer_cannot_reprocess(self, client: AsyncClient) -> None:
        owner = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, owner, "acme")
        collection = await make_collection(client, owner, namespace["id"], "handbook")
        document = await upload_text(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        await signed_in(client, "viewer@example.com")
        from tests.conftest import add_member, auth_header, login

        await add_member(client, owner, namespace["id"], "viewer@example.com", "viewer")
        viewer = auth_header(await login(client, "viewer@example.com"))

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}/reprocess",
            headers=viewer,
        )

        assert response.status_code == 403


# ----------------------------------------------------------------- deletion ---
class TestReliableDeletion:
    async def test_deleting_purges_the_vectors_and_closes_the_record(
        self, client: AsyncClient, store: FakeStore, vector_store: Any
    ) -> None:
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        name = next(iter(store.collections.values())).vector_collection
        assert await vector_store.count(name) > 0

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}", headers=headers
        )

        assert response.status_code == 204
        assert await vector_store.count(name) == 0
        assert store.documents[uuid.UUID(document["id"])].status is DocumentStatus.ARCHIVED
        assert store.events("document.vectors.purged")

    async def test_a_vector_store_outage_does_not_fail_the_users_delete(
        self,
        client: AsyncClient,
        store: FakeStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The soft delete is what makes the document unreachable.

        Retrieval resolves every hit through the database, so an un-purged
        vector cannot be returned or cited. Refusing the delete because the
        index is down would deny the user something they are entitled to in
        order to tidy up a cache.
        """
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        async def unreachable(*_: Any, **__: Any) -> int:
            raise VectorStoreError("the vector store is unreachable")

        monkeypatch.setattr("app.vectorstore.chroma.ChromaVectorStore.delete_document", unreachable)

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}", headers=headers
        )

        assert response.status_code == 204
        # Gone from the user's point of view...
        follow_up = await client.get(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}", headers=headers
        )
        assert follow_up.status_code == 404
        # ...and still on the cleanup queue.
        row = store.documents[uuid.UUID(document["id"])]
        assert row.status is DocumentStatus.DELETING
        delete_job = next(job for job in store.jobs.values() if job.kind is JobKind.DELETE)
        assert delete_job.status is JobStatus.QUEUED

    async def test_the_purge_completes_when_the_store_comes_back(
        self,
        client: AsyncClient,
        store: FakeStore,
        vector_store: Any,
        lifecycle: DocumentLifecycleService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovery from a partial failure, end to end."""
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        name = next(iter(store.collections.values())).vector_collection

        async def unreachable(*_: Any, **__: Any) -> int:
            raise VectorStoreError("the vector store is unreachable")

        monkeypatch.setattr("app.vectorstore.chroma.ChromaVectorStore.delete_document", unreachable)
        await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}", headers=headers
        )
        assert await vector_store.count(name) > 0

        monkeypatch.undo()
        # The failed attempt scheduled its retry a few seconds out. Standing
        # in for that wait rather than sleeping through it; what is being
        # tested is that the retry happens, not that time passes.
        delete_job = next(job for job in store.jobs.values() if job.kind is JobKind.DELETE)
        delete_job.run_after = datetime.now(UTC) - timedelta(seconds=1)
        result = await lifecycle.claim_and_run(owner="worker-1")

        assert result is not None and result.succeeded
        assert await vector_store.count(name) == 0
        assert store.documents[uuid.UUID(document["id"])].status is DocumentStatus.ARCHIVED

    async def test_running_a_delete_job_twice_is_harmless(
        self, lifecycle: DocumentLifecycleService, store: FakeStore, settings: Settings
    ) -> None:
        """Lease expiry can hand one job to two workers; it must converge."""
        document = make_document(store, status=DocumentStatus.DELETING)
        document.deleted_at = datetime.now(UTC)
        job = await lifecycle.enqueue(document, JobKind.DELETE)

        first = await lifecycle.run_now(job)
        job.status = JobStatus.QUEUED
        second = await lifecycle.run_now(job)

        assert first.succeeded and second.succeeded
        assert document.status is DocumentStatus.ARCHIVED

    async def test_an_uploader_cannot_delete_someone_elses_document(
        self, client: AsyncClient
    ) -> None:
        from tests.conftest import add_member, auth_header, login

        owner = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, owner, "acme")
        collection = await make_collection(client, owner, namespace["id"], "handbook")
        document = await upload_text(
            client,
            owner,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        await signed_in(client, "member@example.com")
        await add_member(client, owner, namespace["id"], "member@example.com", "member")
        member = auth_header(await login(client, "member@example.com"))

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}", headers=member
        )

        assert response.status_code == 403


# ------------------------------------------------------- stale vector sweep ---
class TestStaleVectorCleanup:
    async def _collection_name(self, store: FakeStore) -> str:
        return next(iter(store.collections.values())).vector_collection

    async def test_a_vector_with_no_chunk_behind_it_is_removed(
        self,
        client: AsyncClient,
        store: FakeStore,
        vector_store: Any,
        embeddings: Any,
    ) -> None:
        """The backstop for every partial failure in the system.

        A crash between writing vectors and committing chunk rows leaves
        exactly this: an id in the index that the database has never heard
        of.
        """
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        name = await self._collection_name(store)
        live = await vector_store.count(name)

        orphan_id = f"{uuid.uuid4()}:0"
        await vector_store.upsert(
            name,
            [
                VectorRecord(
                    id=orphan_id,
                    embedding=(await embeddings.embed_documents(["orphaned text"]))[0],
                    document="orphaned text",
                    metadata={
                        "namespace_id": namespace["id"],
                        "collection_id": collection["id"],
                        "document_id": str(uuid.uuid4()),
                        "chunk_index": 0,
                    },
                )
            ],
        )
        assert await vector_store.count(name) == live + 1

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}/reconcile",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["vectors_removed"] == 1
        assert await vector_store.count(name) == live

    async def test_reconciliation_leaves_live_vectors_alone(
        self, client: AsyncClient, store: FakeStore, vector_store: Any
    ) -> None:
        """A sweep that removed real data would be far worse than the leak."""
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        for filename, body in (("handbook.txt", HANDBOOK), ("security.txt", SECURITY)):
            await upload_text(
                client,
                headers,
                namespace["id"],
                collection["id"],
                filename=filename,
                body=body,
            )
        name = await self._collection_name(store)
        before = await vector_store.count(name)

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}/reconcile",
            headers=headers,
        )

        assert response.json()["vectors_removed"] == 0
        assert await vector_store.count(name) == before

        # And the collection is still searchable afterwards.
        search = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}/search",
            headers=headers,
            json={"query": "How much annual leave do employees get?"},
        )
        assert search.status_code == 200
        assert search.json()["hits"]

    async def test_vectors_left_by_a_dead_lettered_delete_are_swept(
        self,
        client: AsyncClient,
        store: FakeStore,
        vector_store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reconciliation finishes what a delete job gave up on."""
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        document = await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )

        async def unreachable(*_: Any, **__: Any) -> int:
            raise VectorStoreError("the vector store is unreachable")

        monkeypatch.setattr("app.vectorstore.chroma.ChromaVectorStore.delete_document", unreachable)
        await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/documents/{document['id']}", headers=headers
        )
        monkeypatch.undo()

        name = await self._collection_name(store)
        stranded = await vector_store.count(name)
        assert stranded > 0

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}/reconcile",
            headers=headers,
        )

        assert response.json()["vectors_removed"] == stranded
        assert await vector_store.count(name) == 0

    async def test_the_sweep_pages_through_a_large_index(
        self,
        client: AsyncClient,
        store: FakeStore,
        vector_store: Any,
        embeddings: Any,
        lifecycle: DocumentLifecycleService,
    ) -> None:
        """Deleting shifts the window, so the cursor must not advance blindly.

        With a naive `offset += len(ids)` this test leaves orphans behind:
        every removal pushes an unexamined vector into a page already passed.
        """
        headers = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, headers, "acme")
        collection = await make_collection(client, headers, namespace["id"], "handbook")
        await upload_text(
            client,
            headers,
            namespace["id"],
            collection["id"],
            filename="handbook.txt",
            body=HANDBOOK,
        )
        name = await self._collection_name(store)
        live = await vector_store.count(name)

        vectors = await embeddings.embed_documents([f"orphan {i}" for i in range(12)])
        await vector_store.upsert(
            name,
            [
                VectorRecord(
                    id=f"{uuid.uuid4()}:{i}",
                    embedding=vector,
                    document=f"orphan {i}",
                    metadata={
                        "namespace_id": namespace["id"],
                        "collection_id": collection["id"],
                        "document_id": str(uuid.uuid4()),
                        "chunk_index": i,
                    },
                )
                for i, vector in enumerate(vectors)
            ],
        )

        row = next(iter(store.collections.values()))
        removed = await lifecycle.reconcile_collection(row, batch_size=3)

        assert removed == 12
        assert await vector_store.count(name) == live

    async def test_a_member_cannot_trigger_a_sweep(self, client: AsyncClient) -> None:
        """Reconciliation reads and deletes across every document in a
        collection, so it is an administrative operation."""
        from tests.conftest import add_member, auth_header, login

        owner = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, owner, "acme")
        collection = await make_collection(client, owner, namespace["id"], "handbook")
        await signed_in(client, "member@example.com")
        await add_member(client, owner, namespace["id"], "member@example.com", "member")
        member = auth_header(await login(client, "member@example.com"))

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}/reconcile",
            headers=member,
        )

        assert response.status_code == 403

    async def test_another_tenant_cannot_trigger_a_sweep(self, client: AsyncClient) -> None:
        owner = await signed_in(client, "owner@example.com")
        namespace = await make_namespace(client, owner, "acme")
        collection = await make_collection(client, owner, namespace["id"], "handbook")
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/collections/{collection['id']}/reconcile",
            headers=outsider,
        )

        assert response.status_code == 404


class TestJobModel:
    def test_attempts_remaining_never_goes_negative(self) -> None:
        job = IngestionJob(kind=JobKind.INGEST, attempts=5, max_attempts=3)
        assert job.attempts_remaining == 0
        assert job.can_retry is False

    def test_a_fresh_job_can_retry(self) -> None:
        job = IngestionJob(kind=JobKind.INGEST, attempts=0, max_attempts=3)
        assert job.attempts_remaining == 3
        assert job.can_retry is True

    def test_in_flight_states_are_the_ones_expecting_work(self) -> None:
        """What a "stuck document" sweep would have to look at."""
        in_flight = {status for status in DocumentStatus if status.is_in_flight}
        assert in_flight == {
            DocumentStatus.PENDING,
            DocumentStatus.QUEUED,
            DocumentStatus.PROCESSING,
            DocumentStatus.DELETING,
        }
