"""The durable work queue behind document ingestion and deletion.

A table rather than an in-process queue, because the work it describes
outlives the request that created it and must outlive the process too. If a
worker is killed between extracting a document's text and writing its
vectors, an in-memory queue loses the job and the document sits in
``processing`` forever; a row survives, its lease expires, and another worker
picks it up.

Leases rather than row deletion-on-claim: a claimed job keeps its row, with
``lease_expires_at`` saying when the claim stops being believed. That makes a
crashed worker's work recoverable by time rather than by heartbeat, and it
keeps the history of what was attempted.

Redis or a broker would do this too. Postgres is chosen because the system
already depends on it, because ``FOR UPDATE SKIP LOCKED`` makes claiming
correct under concurrency without a second datastore to operate, and because
a job and the row it is about can then be changed in one transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import (
    JOB_KIND_ENUM,
    JOB_STATUS_ENUM,
    JobKind,
    JobStatus,
    pg_enum,
)


class IngestionJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One unit of durable work against one document."""

    __tablename__ = "ingestion_jobs"

    namespace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("namespaces.id", ondelete="CASCADE"), nullable=False
    )
    # No foreign key to `documents`: a delete job has to be able to outlive
    # the row it is cleaning up after, and a cascade would remove the job at
    # exactly the moment it still has vectors to remove.
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    kind: Mapped[JobKind] = mapped_column(pg_enum(JobKind, JOB_KIND_ENUM), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, JOB_STATUS_ENUM),
        nullable=False,
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )

    #: Not before this instant. Backoff is expressed by moving this forward
    #: rather than by sleeping, so a retry survives a restart.
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    #: Who holds the claim, and until when. A row whose lease has expired is
    #: available again regardless of its status.
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        # The claim query: runnable work, oldest first.
        Index(
            "ix_ingestion_jobs_status_run_after",
            "status",
            "run_after",
        ),
        # "What is outstanding for this document?" - used when enqueuing, to
        # avoid stacking duplicate work.
        Index("ix_ingestion_jobs_document_id", "document_id"),
        Index("ix_ingestion_jobs_namespace_id", "namespace_id"),
        # Only one unfinished job per document and kind. Double-clicking
        # "reprocess" must not queue the work twice; the second enqueue finds
        # the first still outstanding and leaves it alone.
        Index(
            "uq_ingestion_jobs_document_kind_active",
            "document_id",
            "kind",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
    )

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts
