"""Scope slug uniqueness to live rows.

Revision ID: 0003_partial_slug_uniqueness
Revises: 0002_ingestion_jobs

Namespaces and collections are soft-deleted (``deleted_at``), but their slug
uniqueness was enforced by a plain unique constraint over *every* row. The
effect is a slug that can never be reused: deleting the ``handbook``
collection and creating it again fails with a conflict naming a row the user
can no longer see, and no amount of deleting fixes it.

Scoping the index to ``deleted_at IS NULL`` releases the slug on delete while
keeping the uniqueness that actually matters - among the rows that exist.

**Pre-existing duplicates.** Deleting rows never created duplicates (the old
constraint forbade them), so the new index cannot fail on data this
application wrote. It can fail on data someone else inserted, which is the
correct outcome: a silent failure here would leave two live collections
sharing a slug, and the vector collection name is derived from it.

**Downgrade is lossy by nature.** Restoring the global constraint fails if
any slug is shared between a live row and a soft-deleted one - which is
exactly the state this revision exists to allow. The downgrade therefore
hard-deletes the soft-deleted rows that collide, and says so loudly, rather
than appearing to succeed.
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_partial_slug_uniqueness"
down_revision: str | None = "0002_ingestion_jobs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --------------------------------------------------------- namespaces ---
    # The constraint was created by `unique=True` on the column, so Postgres
    # named it conventionally. Dropped with IF EXISTS because a database
    # created from an older autogenerate may have named it differently; the
    # CREATE below is what actually guarantees the end state.
    op.execute("ALTER TABLE namespaces DROP CONSTRAINT IF EXISTS namespaces_slug_key")
    op.execute("DROP INDEX IF EXISTS uq_namespaces_slug")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_namespaces_slug
            ON namespaces (slug)
            WHERE deleted_at IS NULL
        """
    )

    # -------------------------------------------------------- collections ---
    op.execute("ALTER TABLE collections DROP CONSTRAINT IF EXISTS uq_collections_namespace_id_slug")
    op.execute("DROP INDEX IF EXISTS uq_collections_namespace_id_slug")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_collections_namespace_id_slug
            ON collections (namespace_id, slug)
            WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Collisions can only be between a live row and soft-deleted ones, since
    # the live rows are still uniquely constrained among themselves. The
    # soft-deleted ones are what the global constraint cannot tolerate.
    op.execute(
        """
        DELETE FROM collections c
        WHERE c.deleted_at IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM collections other
              WHERE other.namespace_id = c.namespace_id
                AND other.slug = c.slug
                AND other.id <> c.id
                AND (other.deleted_at IS NULL OR other.id < c.id)
          )
        """
    )
    op.execute(
        """
        DELETE FROM namespaces n
        WHERE n.deleted_at IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM namespaces other
              WHERE other.slug = n.slug
                AND other.id <> n.id
                AND (other.deleted_at IS NULL OR other.id < n.id)
          )
        """
    )

    op.execute("DROP INDEX IF EXISTS uq_collections_namespace_id_slug")
    op.execute(
        """
        ALTER TABLE collections
            ADD CONSTRAINT uq_collections_namespace_id_slug UNIQUE (namespace_id, slug)
        """
    )

    op.execute("DROP INDEX IF EXISTS uq_namespaces_slug")
    op.execute("ALTER TABLE namespaces ADD CONSTRAINT namespaces_slug_key UNIQUE (slug)")
