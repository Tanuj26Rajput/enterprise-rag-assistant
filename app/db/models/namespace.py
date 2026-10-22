"""Namespaces: the tenancy boundary, and their membership table."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import NAMESPACE_ROLE_ENUM, NamespaceRole, pg_enum

if TYPE_CHECKING:
    from app.db.models.audit import AuditLog
    from app.db.models.collection import Collection
    from app.db.models.conversation import Conversation
    from app.db.models.document import Document
    from app.db.models.user import User

# Slugs appear in URLs and in the vector-store collection names, so they are
# restricted to a DNS-safe shape: 2-63 characters, lowercase alphanumerics
# and interior hyphens only. Enforced here, in the request schema and by a
# check constraint, so a slug cannot be smuggled in by a path that skips
# the API.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$"


class Namespace(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """An isolated tenant workspace.

    Every collection, document, conversation and audit record hangs off a
    namespace, and every query is filtered by it. This is the row-level
    boundary that keeps one customer's documents out of another's retrieval.
    """

    __tablename__ = "namespaces"

    # Uniqueness lives in a partial index below, not in `unique=True` here:
    # a soft-deleted workspace must not hold its slug hostage.
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # RESTRICT, not CASCADE: deleting a user must never silently destroy a
    # tenant's documents. Ownership has to be transferred first.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Per-tenant overrides for retrieval/LLM behaviour (top_k, model, prompts).
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        Index(
            "uq_namespaces_slug",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    owner: Mapped[User] = relationship(back_populates="owned_namespaces", foreign_keys=[owner_id])
    members: Mapped[list[NamespaceMember]] = relationship(
        back_populates="namespace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    collections: Mapped[list[Collection]] = relationship(
        back_populates="namespace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Documents reach their namespace through the composite key on
    # `collections`, so this convenience view spells out the join. Deletion
    # still cascades: namespace -> collections -> documents.
    documents: Mapped[list[Document]] = relationship(
        back_populates="namespace",
        primaryjoin="Namespace.id == Document.namespace_id",
        foreign_keys="Document.namespace_id",
        viewonly=True,
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="namespace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="namespace")


class NamespaceMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Grants one user a role inside one namespace."""

    __tablename__ = "namespace_members"

    namespace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("namespaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[NamespaceRole] = mapped_column(
        pg_enum(NamespaceRole, NAMESPACE_ROLE_ENUM),
        nullable=False,
        default=NamespaceRole.MEMBER,
        server_default=NamespaceRole.MEMBER.value,
    )
    # Kept for the invitation audit trail; SET NULL so removing the inviter
    # does not revoke the invitee's access.
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "namespace_id", "user_id", name="uq_namespace_members_namespace_id_user_id"
        ),
        # Authorisation checks read "all members of namespace X with role Y",
        # so the index leads with namespace_id.
        Index("ix_namespace_members_namespace_id_role", "namespace_id", "role"),
        Index("ix_namespace_members_user_id", "user_id"),
    )

    namespace: Mapped[Namespace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships", foreign_keys=[user_id])
    invited_by: Mapped[User | None] = relationship(foreign_keys=[invited_by_id])
