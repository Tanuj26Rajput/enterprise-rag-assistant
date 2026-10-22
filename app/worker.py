"""The ingestion worker: claim a job, run it, commit, repeat.

Run with ``python -m app.worker`` (or ``INGESTION_MODE=background`` plus this
process in the container). With ``INGESTION_MODE=inline`` the API does the
same work in-request and this process is unnecessary, which is the point of
having both: a single-container deployment needs no queue consumer, and a
large corpus gets one without the application changing.

Three properties are worth stating explicitly, because they are what make
this safe to run several of:

* **One transaction per job.** The claim, the work and the outcome commit
  together. A worker killed mid-job commits nothing, so the job stays
  claimed, its lease expires, and another worker takes it.
* **No coordination.** Claiming uses ``FOR UPDATE SKIP LOCKED``, so N workers
  against one database divide the queue between them with no leader, lock
  service or broker.
* **Idempotent work.** Because the lease can expire while a job is genuinely
  still running - a 40-minute PDF against a 10-minute lease - two workers can
  briefly run the same job. Re-running converges rather than duplicating:
  chunk rows are replaced, vector ids are derived, counters are deltas.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
import uuid

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, job_id_var
from app.db.session import dispose_engine, session_scope
from app.embeddings.factory import get_embedding_provider
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.repositories.document import DocumentChunkRepository, DocumentRepository
from app.repositories.job import JobRepository
from app.repositories.namespace import NamespaceMemberRepository, NamespaceRepository
from app.services.authorization import NamespaceAuthorizer
from app.services.document import DocumentService
from app.services.indexing import IndexingService
from app.services.lifecycle import DocumentLifecycleService
from app.services.storage import LocalFileStorage
from app.vectorstore.chroma import ChromaVectorStore

logger = logging.getLogger(__name__)


def worker_name() -> str:
    """A lease owner that identifies the process in `ingestion_jobs`.

    Host and pid, so an operator looking at a stuck lease can find the
    process that holds it; a random suffix because container hostnames and
    pids both repeat.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _build_lifecycle(session, settings: Settings) -> DocumentLifecycleService:
    """Assemble the same object graph the API builds through dependencies.

    Written out rather than reused from ``app.api.deps`` so the worker does
    not import FastAPI: the queue consumer has no HTTP surface and should not
    acquire one by accident.
    """
    documents = DocumentRepository(session)
    chunks = DocumentChunkRepository(session)
    collections = CollectionRepository(session)
    audit = AuditRepository(session)
    authorizer = NamespaceAuthorizer(
        NamespaceRepository(session), NamespaceMemberRepository(session)
    )
    indexing = IndexingService(
        ChromaVectorStore(
            persist_dir=settings.chroma_persist_dir, distance=settings.chroma_distance
        ),
        get_embedding_provider(),
        settings,
    )
    storage = LocalFileStorage(settings.upload_dir)
    processor = DocumentService(
        documents,
        chunks,
        collections,
        audit,
        authorizer,
        storage,
        settings,
        indexing,
    )
    return DocumentLifecycleService(
        JobRepository(session),
        documents,
        chunks,
        collections,
        audit,
        authorizer,
        indexing,
        settings,
        processor,
        storage,
    )


async def run_once(owner: str, settings: Settings) -> bool:
    """Claim and run at most one job. True if there was work to do."""
    async with session_scope() as session:
        lifecycle = _build_lifecycle(session, settings)
        result = await lifecycle.claim_and_run(owner=owner)

    if result is None:
        return False

    # Set after the fact rather than around the claim: the job's identity is
    # not known until it has been claimed, and a token set inside the
    # `session_scope` block would be reset before this line runs.
    token = job_id_var.set(str(result.job_id))
    try:
        logger.info(
            "Job %s (%s) finished: %s after attempt %d%s",
            result.job_id,
            result.kind.value,
            result.status.value,
            result.attempts,
            f" - {result.error}" if result.error else "",
            extra={
                "job_kind": result.kind.value,
                "job_status": result.status.value,
                "attempts": result.attempts,
                "worker": owner,
            },
        )
    finally:
        job_id_var.reset(token)
    return True


async def recover(settings: Settings) -> int:
    """Hand back work abandoned by a worker that stopped.

    Run once at start-up: a process killed mid-job leaves its row marked
    running, and nothing else will ever move it.
    """
    async with session_scope() as session:
        return await _build_lifecycle(session, settings).recover_abandoned()


async def serve(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    owner = worker_name()
    stopping = asyncio.Event()

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            # Not implemented on Windows' selector loop; there SIGINT still
            # arrives as KeyboardInterrupt, handled by the caller.
            loop.add_signal_handler(sig, stopping.set)

    logger.info("Ingestion worker %s starting", owner)
    recovered = await recover(settings)
    if recovered:
        logger.info("Requeued %d job(s) abandoned by a previous worker", recovered)

    while not stopping.is_set():
        try:
            busy = await run_once(owner, settings)
        except Exception:
            # A failure here is infrastructural - the database is unreachable,
            # the session is broken. The job's own failures never reach this
            # far. Back off rather than spin, and keep the worker alive.
            logger.exception("Worker loop failed; backing off")
            busy = False

        if busy:
            # More work is likely waiting; take it without pausing.
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=settings.ingestion_poll_seconds)

    logger.info("Ingestion worker %s stopping", owner)
    await dispose_engine()


def main() -> None:
    settings = get_settings()
    # The same formatter the API uses, so one log pipeline ingests both and a
    # `request_id` filter follows an upload from the HTTP call that accepted
    # it into the worker that processed it.
    configure_logging(settings.log_level, json_output=settings.log_json)

    if sys.platform == "win32":
        # psycopg's async driver cannot run on the ProactorEventLoop; see the
        # note in run.py.
        with (
            asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner,
            contextlib.suppress(KeyboardInterrupt),
        ):
            runner.run(serve(settings))
        return

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
