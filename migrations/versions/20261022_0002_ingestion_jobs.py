"""Durable ingestion queue, and the two document states it introduces.

Revision ID: 0002_ingestion_jobs
Revises: 0001_initial_schema
Create Date: 2026-10-22

Two things happen here.

``document_status`` gains ``queued`` and ``deleting``. PostgreSQL can add
enum values but cannot remove them, so :func:`downgrade` cannot simply undo
this - it rebuilds the type from scratch, after moving any row still sitting
in a departing state onto its closest surviving neighbour. That is why the
downgrade is long: the alternative is a downgrade that fails on live data.

``ingestion_jobs`` is the queue itself. Note the partial unique index: one
*unfinished* job per document and kind, which is what makes "enqueue" safe to
call twice. A plain unique index would also forbid the second attempt a week
later, and a table with no constraint at all would let a double-clicked
button queue two jobs that then race to rewrite the same chunks.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ingestion_jobs"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JOB_KIND = ("ingest", "reindex", "delete")
JOB_STATUS = ("queued", "running", "succeeded", "failed", "cancelled")

#: `document_status` before and after this revision.
DOCUMENT_STATUS_OLD = ("pending", "processing", "ready", "failed", "archived")
DOCUMENT_STATUS_NEW = (
    "pending",
    "queued",
    "processing",
    "ready",
    "failed",
    "deleting",
    "archived",
)


def _enum(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    """Reference an already-created enum type without re-emitting CREATE TYPE."""
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    for name, values in (("job_kind", JOB_KIND), ("job_status", JOB_STATUS)):
        rendered = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    # ADD VALUE is not transactional before PG 12 and cannot be used in the
    # same transaction as a table that then references the new value; adding
    # them first, in their own statements, keeps this working on every
    # supported server.
    op.execute("ALTER TYPE document_status ADD VALUE IF NOT EXISTS 'queued' AFTER 'pending'")
    op.execute("ALTER TYPE document_status ADD VALUE IF NOT EXISTS 'deleting' BEFORE 'archived'")

    op.create_table(
        "ingestion_jobs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("namespace_id", sa.Uuid(as_uuid=True), nullable=False),
        # Deliberately no foreign key: a delete job must outlive the document
        # row it is cleaning up after, and a cascade would remove the job at
        # exactly the moment it still has vectors to purge.
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", _enum("job_kind", JOB_KIND), nullable=False),
        sa.Column(
            "status",
            _enum("job_status", JOB_STATUS),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_ingestion_jobs_attempts_non_negative")),
        sa.CheckConstraint(
            "max_attempts > 0", name=op.f("ck_ingestion_jobs_max_attempts_positive")
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts", name=op.f("ck_ingestion_jobs_attempts_within_limit")
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespaces.id"],
            name=op.f("fk_ingestion_jobs_namespace_id_namespaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_jobs")),
    )
    # The claim query: runnable work, oldest first.
    op.create_index("ix_ingestion_jobs_status_run_after", "ingestion_jobs", ["status", "run_after"])
    op.create_index("ix_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"])
    op.create_index("ix_ingestion_jobs_namespace_id", "ingestion_jobs", ["namespace_id"])
    # One *outstanding* job per document and kind. Partial, so a document can
    # be re-ingested any number of times over its life.
    op.create_index(
        "uq_ingestion_jobs_document_kind_active",
        "ingestion_jobs",
        ["document_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_ingestion_jobs_document_kind_active", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_namespace_id", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_document_id", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_status_run_after", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.execute("DROP TYPE job_status")
    op.execute("DROP TYPE job_kind")

    # PostgreSQL has no DROP VALUE, so the type is rebuilt. Rows in a state
    # that is going away are moved first: `queued` is a document waiting to
    # be processed, which is what `pending` meant before this revision, and
    # `deleting` is a document on its way out, which `archived` covered.
    op.execute("ALTER TABLE documents ALTER COLUMN status DROP DEFAULT")
    op.execute("UPDATE documents SET status = 'pending' WHERE status = 'queued'")
    op.execute("UPDATE documents SET status = 'archived' WHERE status = 'deleting'")
    op.execute("ALTER TYPE document_status RENAME TO document_status_old")
    rendered = ", ".join(f"'{value}'" for value in DOCUMENT_STATUS_OLD)
    op.execute(f"CREATE TYPE document_status AS ENUM ({rendered})")
    op.execute(
        "ALTER TABLE documents ALTER COLUMN status TYPE document_status "
        "USING status::text::document_status"
    )
    op.execute("ALTER TABLE documents ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute("DROP TYPE document_status_old")
