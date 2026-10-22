"""Append-only audit trail."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

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
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import (
    AUDIT_ACTION_ENUM,
    AUDIT_OUTCOME_ENUM,
    AuditAction,
    AuditOutcome,
    pg_enum,
)

if TYPE_CHECKING:
    from app.db.models.namespace import Namespace
    from app.db.models.user import User


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """One recorded security-relevant operation.

    Rows are written once and never updated or deleted, which is why this
    model carries only ``created_at``.

    Both the actor and the namespace are nullable with ``ON DELETE SET NULL``:
    an audit trail that disappears when the user it incriminates is deleted is
    not an audit trail. ``actor_email`` and ``resource_label`` are denormalised
    copies so an entry stays readable after its referents are gone.
    """

    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    namespace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("namespaces.id", ondelete="SET NULL"), nullable=True
    )

    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, AUDIT_ACTION_ENUM), nullable=False
    )
    # Fully-qualified operation name, e.g. "document.upload". The enum above
    # is the coarse bucket this rolls up to.
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[AuditOutcome] = mapped_column(
        pg_enum(AuditOutcome, AUDIT_OUTCOME_ENUM),
        nullable=False,
        default=AuditOutcome.SUCCESS,
        server_default=AuditOutcome.SUCCESS.value,
    )

    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Deliberately not a foreign key: the trail must outlive the row it
    # describes, and it points at many different tables.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    resource_label: Mapped[str | None] = mapped_column(String(512), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Correlates an audit entry with the application log line and trace for
    # the same request.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    http_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Diffs and request context. Never store secrets or raw PII here.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # Compliance export: everything that happened in a namespace, newest
        # first. This is the query the table exists for.
        Index(
            "ix_audit_logs_namespace_id_created_at",
            "namespace_id",
            text("created_at DESC"),
        ),
        Index("ix_audit_logs_actor_id_created_at", "actor_id", text("created_at DESC")),
        # "What happened to this document?"
        Index("ix_audit_logs_resource_type_resource_id", "resource_type", "resource_id"),
        Index("ix_audit_logs_event_created_at", "event", text("created_at DESC")),
        Index("ix_audit_logs_request_id", "request_id"),
        CheckConstraint(
            "http_status IS NULL OR (http_status BETWEEN 100 AND 599)",
            name="http_status_in_range",
        ),
        CheckConstraint(
            "(resource_id IS NULL) OR (resource_type IS NOT NULL)",
            name="resource_id_requires_type",
        ),
    )

    actor: Mapped[User | None] = relationship(back_populates="audit_logs")
    namespace: Mapped[Namespace | None] = relationship(back_populates="audit_logs")
