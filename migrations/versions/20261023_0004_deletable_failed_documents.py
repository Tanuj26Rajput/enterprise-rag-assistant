"""Let a failed document be deleted.

Revision ID: 0004_deletable_failed_documents
Revises: 0003_partial_slug_uniqueness

``ck_documents_error_message_matches_status`` was written as a biconditional:

    (status = 'failed') = (error_message IS NOT NULL)

which says two things at once - a failed document must explain itself, and
nothing else may carry an error. The first is right. The second is too strong,
because it also governs the two states a document passes through on its way
out.

Deleting a document sets ``deleting`` and then ``archived`` while leaving
``error_message`` where it is. For a document that had failed, that UPDATE
violated the constraint, so the delete returned 500 and **no failed document
could ever be removed** - exactly the ones a user most wants gone. A scanned
PDF that could not be OCR'd was unremovable for the life of the deployment.

Clearing the message on delete would also satisfy the old rule, and is the
wrong trade: it destroys the answer to "why is this gone?" at the moment that
question is being asked. The constraint is narrowed instead, so it governs the
live states and stops governing a tombstone.

Rewritten rather than dropped, because the half that was right is worth
keeping: a ``ready`` document carrying a stale error is a real defect, and
without the ELSE branch nothing would catch it.
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_deletable_failed_documents"
down_revision: str | None = "0003_partial_slug_uniqueness"
branch_labels: str | None = None
depends_on: str | None = None

CONSTRAINT = "ck_documents_error_message_matches_status"

NEW_CONDITION = """
    CASE status
        WHEN 'failed' THEN error_message IS NOT NULL
        WHEN 'deleting' THEN TRUE
        WHEN 'archived' THEN TRUE
        ELSE error_message IS NULL
    END
"""

OLD_CONDITION = "(status = 'failed') = (error_message IS NOT NULL)"


def upgrade() -> None:
    # Dropped and recreated rather than altered: PostgreSQL has no
    # ALTER CONSTRAINT that can change a CHECK's expression.
    op.execute(f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(f"ALTER TABLE documents ADD CONSTRAINT {CONSTRAINT} CHECK ({NEW_CONDITION})")


def downgrade() -> None:
    """Restore the stricter rule, clearing the rows that cannot satisfy it.

    The old constraint cannot be re-added while any tombstone still carries
    the explanation this revision exists to preserve, and PostgreSQL validates
    a CHECK against existing rows when it is added. The messages on those rows
    are therefore discarded first.

    This is lossy, and deliberately so rather than silently: the alternative
    is a downgrade that fails on any database where a failed document has been
    deleted, which is every database where this revision was useful.
    """
    op.execute(
        """
        UPDATE documents
           SET error_message = NULL
         WHERE status IN ('deleting', 'archived')
           AND error_message IS NOT NULL
        """
    )
    op.execute(f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(f"ALTER TABLE documents ADD CONSTRAINT {CONSTRAINT} CHECK ({OLD_CONDITION})")
