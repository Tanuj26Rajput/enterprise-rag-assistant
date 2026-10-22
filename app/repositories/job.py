"""Persistence for the ingestion queue.

The interesting query here is :meth:`JobRepository.claim`. Two workers polling
the same table will otherwise either hand the same job to both (no locking) or
serialise behind each other (plain ``FOR UPDATE``). ``SKIP LOCKED`` is what
makes the queue concurrent: each worker takes the oldest row nobody else has
locked, and no coordination outside the database is needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IngestionJob, JobKind, JobStatus

#: Statuses a job can hold while it still represents outstanding work.
ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None:
        return await self._session.scalar(select(IngestionJob).where(IngestionJob.id == job_id))

    async def active_for_document(
        self, document_id: uuid.UUID, kind: JobKind
    ) -> IngestionJob | None:
        """An outstanding job of this kind, if one already exists.

        Backed by a partial unique index, so this is an optimisation of the
        common case rather than the thing preventing duplicates.
        """
        return await self._session.scalar(
            select(IngestionJob).where(
                IngestionJob.document_id == document_id,
                IngestionJob.kind == kind,
                IngestionJob.status.in_(ACTIVE_STATUSES),
            )
        )

    async def list_for_document(
        self, document_id: uuid.UUID, *, limit: int = 20
    ) -> list[IngestionJob]:
        rows = await self._session.scalars(
            select(IngestionJob)
            .where(IngestionJob.document_id == document_id)
            .order_by(IngestionJob.created_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def add(self, job: IngestionJob) -> IngestionJob:
        self._session.add(job)
        await self._session.flush()
        return job

    async def claim(self, *, owner: str, lease_seconds: int) -> IngestionJob | None:
        """Take the oldest runnable job, or return None.

        A job is runnable when it is queued and due, *or* when it is marked
        running but its lease has expired - which is how a worker that died
        mid-job gives the work back without anyone having to notice that it
        died.
        """
        now = datetime.now(UTC)
        expired = IngestionJob.lease_expires_at.is_(None) | (IngestionJob.lease_expires_at < now)

        candidate = (
            select(IngestionJob.id)
            .where(
                IngestionJob.run_after <= now,
                or_(
                    IngestionJob.status == JobStatus.QUEUED,
                    (IngestionJob.status == JobStatus.RUNNING) & expired,
                ),
            )
            .order_by(IngestionJob.run_after, IngestionJob.created_at)
            .limit(1)
            # Skipping locked rows is what lets several workers poll the same
            # table without blocking one another.
            .with_for_update(skip_locked=True)
        )

        job_id = await self._session.scalar(candidate)
        if job_id is None:
            return None

        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(
                status=JobStatus.RUNNING,
                attempts=IngestionJob.attempts + 1,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=func.coalesce(IngestionJob.started_at, now),
                updated_at=now,
            )
        )
        return await self.get(job_id)

    async def extend_lease(self, job: IngestionJob, *, lease_seconds: int) -> None:
        """Push the lease out while a long job is still making progress."""
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        await self._session.flush()

    async def mark_succeeded(self, job: IngestionJob) -> None:
        job.status = JobStatus.SUCCEEDED
        job.finished_at = datetime.now(UTC)
        job.last_error = None
        job.lease_owner = None
        job.lease_expires_at = None
        await self._session.flush()

    async def mark_failed(self, job: IngestionJob, error: str, *, retry_in: float | None) -> None:
        """Requeue with backoff, or give up and leave a dead letter.

        ``retry_in`` is None when the caller has decided the failure is not
        worth another attempt - a corrupt file does not become readable on
        the third try, and retrying it burns the budget that a genuinely
        transient failure needs.
        """
        job.last_error = error[:2000]
        job.lease_owner = None
        job.lease_expires_at = None

        if retry_in is None or not job.can_retry:
            job.status = JobStatus.FAILED
            job.finished_at = datetime.now(UTC)
        else:
            job.status = JobStatus.QUEUED
            job.run_after = datetime.now(UTC) + timedelta(seconds=retry_in)
        await self._session.flush()

    async def release_expired(self, *, older_than_seconds: int = 0) -> int:
        """Return abandoned jobs to the queue.

        Called on worker start-up as well as periodically: a process that was
        killed leaves rows marked running, and nothing else will ever move
        them. Jobs that have also exhausted their attempts are failed rather
        than requeued, so a job that reliably kills its worker cannot loop
        forever.
        """
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=older_than_seconds)

        stale = select(IngestionJob).where(
            IngestionJob.status == JobStatus.RUNNING,
            IngestionJob.lease_expires_at.is_not(None),
            IngestionJob.lease_expires_at < cutoff,
        )
        rows = list(await self._session.scalars(stale))
        for job in rows:
            job.lease_owner = None
            job.lease_expires_at = None
            if job.can_retry:
                job.status = JobStatus.QUEUED
                job.run_after = now
            else:
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.last_error = (
                    job.last_error or "The worker holding this job stopped without reporting."
                )
        await self._session.flush()
        return len(rows)

    async def counts_by_status(self, namespace_id: uuid.UUID | None = None) -> dict[str, int]:
        """Queue depth per status, for the metrics endpoint."""
        stmt = select(IngestionJob.status, func.count()).group_by(IngestionJob.status)
        if namespace_id is not None:
            stmt = stmt.where(IngestionJob.namespace_id == namespace_id)
        rows = await self._session.execute(stmt)
        return {
            str(status.value if hasattr(status, "value") else status): int(count)
            for status, count in rows
        }

    async def flush(self) -> None:
        await self._session.flush()
