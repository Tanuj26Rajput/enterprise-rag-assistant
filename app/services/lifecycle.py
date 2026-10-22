"""The document lifecycle: queueing, retrying, recovering and reconciling.

This module owns the question "what happens when ingestion goes wrong", which
the ingestion pipeline itself deliberately does not answer. The pipeline
extracts, chunks and indexes; this decides whether a failure deserves another
attempt, when, and what the document looks like in the meantime.

**Two execution modes, one code path.** A job row is written whichever mode is
configured. In ``inline`` mode the request runs it immediately, so a small
deployment needs no worker process; in ``background`` mode a worker claims it.
Either way the durable record exists before the work starts, so a crash leaves
something to recover *from* rather than a document stuck in ``processing``
with nothing pointing at it.

**Retries are bounded and classified.** A corrupt PDF does not become readable
on the third attempt, so parse and validation failures are dead-lettered at
once; a provider timeout is retried with exponential backoff. Spending three
attempts on a permanent failure is not caution, it is latency.

**Idempotence throughout.** Vector ids are derived from
``(document_id, chunk_index)``, chunk rows are replaced wholesale, and the
collection's counters are recomputed from a delta rather than blindly
incremented. Running any job twice therefore converges on the same state,
which is what makes "retry after an unknown failure" a safe default rather
than a way to double-count.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.exceptions import NotFound, ValidationFailed
from app.core.metrics import record_document_ingested, record_job
from app.db.models import (
    AuditAction,
    AuditOutcome,
    Collection,
    Document,
    DocumentStatus,
    IngestionJob,
    JobKind,
    JobStatus,
    NamespaceRole,
    User,
)
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.repositories.document import DocumentChunkRepository, DocumentRepository
from app.repositories.job import JobRepository
from app.services.authorization import NamespaceAuthorizer
from app.services.indexing import IndexingService
from app.services.storage import BlobStorage

logger = logging.getLogger(__name__)

#: Failures another attempt cannot fix. The file is what it is: a password
#: protected PDF, an empty document, a mime type nothing can parse.
PERMANENT_ERRORS: tuple[type[Exception], ...] = (ValidationFailed, KeyError)


@dataclass(frozen=True, slots=True)
class JobResult:
    """What one execution of a job did. Returned instead of raised."""

    job_id: uuid.UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is JobStatus.SUCCEEDED

    @property
    def will_retry(self) -> bool:
        return self.status is JobStatus.QUEUED


def backoff_seconds(attempt: int, *, base: float = 5.0, cap: float = 600.0) -> float:
    """Delay before the attempt after ``attempt``.

    Deterministic, unlike the in-request retry helper: the delay is persisted
    in ``run_after``, so it survives a restart. Jitter is left out because
    workers poll at independent moments anyway - the thundering herd that
    jitter protects against does not form here.
    """
    return min(cap, base * (2 ** max(0, attempt - 1)))


class DocumentLifecycleService:
    """Queues, runs, recovers and reconciles document work.

    Two kinds of method live here, and the distinction matters. The ones
    taking a ``User`` are request-facing and authorize first. The ones taking
    a job or a document are the machinery underneath: they have no actor and
    perform no access control, because a worker is not acting on anyone's
    behalf - it is finishing work that was already authorized when it was
    queued.
    """

    def __init__(
        self,
        jobs: JobRepository,
        documents: DocumentRepository,
        chunks: DocumentChunkRepository,
        collections: CollectionRepository,
        audit: AuditRepository,
        authorizer: NamespaceAuthorizer,
        indexing: IndexingService | None,
        settings: Settings,
        processor: object | None = None,
        storage: BlobStorage | None = None,
    ) -> None:
        self._jobs = jobs
        self._documents = documents
        self._chunks = chunks
        self._collections = collections
        self._audit = audit
        self._authorizer = authorizer
        self._indexing = indexing
        self._settings = settings
        # The ingestion pipeline (a DocumentService). Injected rather than
        # imported so the dependency runs one way: the lifecycle drives
        # ingestion, ingestion knows nothing about jobs.
        self._processor = processor
        # Deleting a document has to remove the stored bytes too, not just
        # the index entry: a user who deletes a document expects the file to
        # be gone, and an upload directory that only ever grows is a
        # retention problem as much as a disk one.
        self._storage = storage

    @property
    def inline(self) -> bool:
        """Whether a freshly queued job should run in this request."""
        return self._settings.ingestion_mode == "inline"

    # ------------------------------------------------------------- enqueue ---
    async def enqueue(
        self,
        document: Document,
        kind: JobKind,
        *,
        max_attempts: int | None = None,
        delay_seconds: float = 0.0,
    ) -> IngestionJob:
        """Record work to be done, or return the job already recording it.

        Idempotent on purpose: a double-clicked "reprocess", or a retried
        request, must not produce two jobs racing to rewrite one document's
        chunks. The partial unique index enforces that; this check is what
        turns the race into a quiet no-op instead of an integrity error.
        """
        existing = await self._jobs.active_for_document(document.id, kind)
        if existing is not None:
            logger.debug("Job %s already outstanding for document %s", existing.id, document.id)
            return existing

        job = IngestionJob(
            namespace_id=document.namespace_id,
            document_id=document.id,
            kind=kind,
            status=JobStatus.QUEUED,
            max_attempts=max_attempts or self._settings.ingestion_max_attempts,
            run_after=datetime.now(UTC) + timedelta(seconds=delay_seconds),
        )
        await self._jobs.add(job)

        if kind in (JobKind.INGEST, JobKind.REINDEX):
            # `queued` rather than `pending`: the difference between "nothing
            # has claimed this yet" and "nobody ever will" is the whole point
            # of having a durable queue, and the UI shows it.
            document.status = DocumentStatus.QUEUED
            document.error_message = None
            await self._documents.flush()

        logger.info("Queued %s job %s for document %s", kind.value, job.id, document.id)
        return job

    async def enqueue_and_maybe_run(
        self, document: Document, kind: JobKind
    ) -> tuple[IngestionJob, JobResult | None]:
        """Queue the work, and in ``inline`` mode do it now.

        The single place the two deployment modes diverge. Everything below
        it is identical either way, which is what keeps inline mode a
        deployment choice rather than a second implementation with its own
        bugs.
        """
        job = await self.enqueue(document, kind)
        if not self.inline or job.status is not JobStatus.QUEUED:
            return job, None
        return job, await self.run_now(job)

    # ----------------------------------------------------------------- run ---
    async def run(self, job: IngestionJob) -> JobResult:
        """Execute one job and record its outcome.

        Never raises for a failure of the work itself: the failure *is* the
        result, written to the job and to the document. A raise here would
        take down a worker loop over one unreadable file.
        """
        document = await self._documents.get_any(job.document_id)
        if document is None:
            # The row is gone entirely - a hard delete, or a namespace
            # cascade. Nothing to do and nothing to retry; the job is
            # complete in the only sense available to it.
            logger.info("Job %s refers to a document that no longer exists", job.id)
            await self._jobs.mark_succeeded(job)
            return JobResult(job.id, job.kind, JobStatus.SUCCEEDED, job.attempts)

        started = time.perf_counter()
        try:
            if job.kind is JobKind.DELETE:
                await self._run_delete(document)
            else:
                await self._run_ingest(document)
        except Exception as exc:
            result = await self._record_failure(job, document, exc)
            # Dead-lettered and will-retry are different operational events:
            # one needs a human, the other is the system absorbing a blip.
            record_job(
                job.kind.value,
                "retry" if result.will_retry else "failed",
                time.perf_counter() - started,
            )
            return result

        await self._jobs.mark_succeeded(job)
        record_job(job.kind.value, "succeeded", time.perf_counter() - started)
        if job.kind is JobKind.INGEST:
            record_document_ingested("succeeded", chunks=document.chunk_count or 0)
        return JobResult(job.id, job.kind, JobStatus.SUCCEEDED, job.attempts)

    async def run_now(self, job: IngestionJob) -> JobResult:
        """Run a job in the current request, as ``inline`` mode does.

        Marks it running and counts the attempt first, so the attempt
        accounting, the dead-lettering and the recovery behaviour are exactly
        the worker's.
        """
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.started_at = job.started_at or datetime.now(UTC)
        await self._jobs.flush()
        return await self.run(job)

    async def claim_and_run(self, *, owner: str) -> JobResult | None:
        """Take one job from the queue and run it. ``None`` when idle."""
        job = await self._jobs.claim(
            owner=owner, lease_seconds=self._settings.ingestion_lease_seconds
        )
        if job is None:
            return None
        return await self.run(job)

    async def _run_ingest(self, document: Document) -> None:
        if self._processor is None:  # pragma: no cover - wiring error
            raise RuntimeError("No ingestion processor is configured.")
        await self._processor.process(document, reraise=True)  # type: ignore[attr-defined]

    async def _run_delete(self, document: Document) -> None:
        """Purge a deleted document's vectors, then close the record.

        Safe to run twice: deleting by document id matches nothing the second
        time, and the status is set rather than decremented.
        """
        collection = await self._collections.get_any(document.collection_id)
        removed = 0
        if self._indexing is not None and collection is not None:
            removed = await self._indexing.remove_document(collection, document.id)

        if self._storage is not None and document.storage_uri:
            # Idempotent: deleting a file that is already gone is a no-op, so
            # a retried job does not fail on its own earlier success.
            await self._storage.delete(document.storage_uri)

        document.status = DocumentStatus.ARCHIVED
        await self._documents.flush()
        await self._audit.record(
            action=AuditAction.DELETE,
            event="document.vectors.purged",
            namespace_id=document.namespace_id,
            resource_type="document",
            resource_id=document.id,
            resource_label=document.filename,
            metadata={"vectors_removed": removed, "blob_removed": self._storage is not None},
        )

    async def _record_failure(
        self, job: IngestionJob, document: Document, exc: Exception
    ) -> JobResult:
        permanent = isinstance(exc, PERMANENT_ERRORS)
        message = exc.message if isinstance(exc, ValidationFailed) else str(exc)
        if not message:
            message = exc.__class__.__name__
        if not permanent:
            logger.warning("Job %s failed: %s", job.id, exc, exc_info=True)

        await self._jobs.mark_failed(
            job, message, retry_in=None if permanent else backoff_seconds(job.attempts)
        )
        dead = job.status is JobStatus.FAILED

        if job.kind in (JobKind.INGEST, JobKind.REINDEX):
            # Only a dead-lettered job leaves the document `failed`. While
            # attempts remain it goes back to `queued`, which is both honest
            # and useful: an operator's "needs attention" list should not
            # fill up with work that is already scheduled to be retried.
            document.status = DocumentStatus.FAILED if dead else DocumentStatus.QUEUED
            document.error_message = message
            await self._documents.flush()

        await self._audit.record(
            action=AuditAction.UPDATE,
            event="document.job.failed" if dead else "document.job.retry",
            outcome=AuditOutcome.FAILURE,
            namespace_id=document.namespace_id,
            resource_type="document",
            resource_id=document.id,
            resource_label=document.filename,
            error_message=message,
            metadata={
                "job_id": str(job.id),
                "kind": job.kind.value,
                "attempt": job.attempts,
                "max_attempts": job.max_attempts,
                "permanent": permanent,
            },
        )
        return JobResult(job.id, job.kind, job.status, job.attempts, message)

    # ------------------------------------------------------------ recovery ---
    async def recover_abandoned(self) -> int:
        """Return jobs whose worker vanished to the queue.

        Run at worker start-up and periodically. Without it a ``SIGKILL``
        mid-ingestion leaves a row marked running that nothing will ever
        claim again, and a document queued behind it forever.
        """
        released = await self._jobs.release_expired()
        if released:
            logger.info("Recovered %d abandoned job(s)", released)
        return released

    # -------------------------------------------------------- request-side ---
    async def reprocess(
        self, user: User, namespace_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[Document, IngestionJob]:
        """Re-run ingestion for a document, at a user's request.

        Also the retry path for a dead-lettered job: the old job stays as the
        record of what failed, and a fresh one gets a full attempt budget.
        Re-running is safe at any point because every step is idempotent -
        the chunk rows are replaced, and the vector ids are derived from the
        document rather than generated.
        """
        context = await self._authorizer.authorize(user, namespace_id)
        context.require_write()

        document = await self._documents.get(namespace_id, document_id)
        if document is None:
            raise NotFound("Document not found.")

        job, _ = await self.enqueue_and_maybe_run(document, JobKind.REINDEX)
        return document, job

    async def jobs_for_document(
        self, user: User, namespace_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[IngestionJob]:
        """A document's job history - what was attempted, and what failed."""
        await self._authorizer.authorize(user, namespace_id)
        document = await self._documents.get(namespace_id, document_id)
        if document is None:
            raise NotFound("Document not found.")
        return await self._jobs.list_for_document(document.id)

    async def reconcile(self, user: User, namespace_id: uuid.UUID, collection_id: uuid.UUID) -> int:
        """Admin-triggered stale vector sweep over one collection."""
        context = await self._authorizer.authorize(user, namespace_id)
        context.require(NamespaceRole.ADMIN)

        collection = await self._collections.get(namespace_id, collection_id)
        if collection is None:
            raise NotFound("Collection not found.")

        removed = await self.reconcile_collection(collection)
        await self._audit.record(
            action=AuditAction.DELETE,
            event="collection.reconcile",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="collection",
            resource_id=collection.id,
            resource_label=collection.slug,
            metadata={"vectors_removed": removed},
        )
        return removed

    # --------------------------------------------------------- reconciling ---
    async def reconcile_collection(self, collection: Collection, *, batch_size: int = 0) -> int:
        """Delete vectors with no live chunk behind them.

        The backstop for every partial failure in the system: a crash between
        writing vectors and committing chunk rows, a delete job that was
        dead-lettered, a re-process that shrank a document while the store
        was unreachable.

        It is a sweep rather than part of the request path because an
        orphaned vector is never a correctness problem - retrieval resolves
        every hit back through the database, which will not return a chunk
        that is gone. It is a cost and size problem, and those can be fixed
        on a schedule.
        """
        if self._indexing is None:
            return 0

        batch = batch_size or self._settings.ingestion_reconcile_batch
        removed = 0
        offset = 0
        while True:
            ids = await self._indexing.list_vector_ids(collection, limit=batch, offset=offset)
            if not ids:
                break

            live = await self._chunks.get_by_vector_ids(collection.namespace_id, ids)
            orphans = [vector_id for vector_id in ids if vector_id not in live]
            if orphans:
                removed += await self._indexing.remove_vectors(collection, orphans)
                logger.info(
                    "Reconciliation removed %d orphaned vector(s) from collection %s",
                    len(orphans),
                    collection.id,
                )

            if len(ids) < batch:
                break
            # Deleting shifts the window, so the cursor advances only by the
            # rows that survived this page. Without that, every deletion
            # would skip as many unexamined vectors as it removed.
            offset += len(ids) - len(orphans)
        return removed
