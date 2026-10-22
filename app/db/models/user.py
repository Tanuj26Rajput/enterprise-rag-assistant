"""User accounts."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.audit import AuditLog
    from app.db.models.conversation import Conversation
    from app.db.models.document import Document
    from app.db.models.namespace import Namespace, NamespaceMember


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated principal.

    Users are global; their authority is always scoped to a namespace through
    :class:`~app.db.models.namespace.NamespaceMember`. ``is_superuser`` is the
    single exception and is reserved for platform operators.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Emails are normalised on write (see `_normalise_email`); the database
        # enforces it so the unique index stays case-insensitive in practice.
        CheckConstraint("email = lower(email)", name="email_is_lowercase"),
        CheckConstraint("position('@' in email) > 1", name="email_has_local_part"),
    )

    owned_namespaces: Mapped[list[Namespace]] = relationship(
        back_populates="owner",
        foreign_keys="Namespace.owner_id",
    )
    memberships: Mapped[list[NamespaceMember]] = relationship(
        back_populates="user",
        foreign_keys="NamespaceMember.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    uploaded_documents: Mapped[list[Document]] = relationship(back_populates="uploaded_by")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="actor")

    @validates("email")
    def _normalise_email(self, _key: str, value: str) -> str:
        return value.strip().lower()
