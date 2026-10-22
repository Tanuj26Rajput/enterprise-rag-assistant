"""Document collections: the retrieval unit inside a namespace."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.namespace import SLUG_PATTERN

if TYPE_CHECKING:
    from app.db.models.document import Document
    from app.db.models.namespace import Namespace
    from app.db.models.user import User


class Collection(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A named group of documents that a query can be scoped to.

    One collection maps to exactly one vector-store collection. Keeping that
    mapping explicit in ``vector_collection`` means the embedding store can be
    rebuilt or swapped without guessing at naming rules, and a collection can
    be re-embedded under a new model by pointing it at a new vector collection.
    """

    __tablename__ = "collections"

    namespace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("namespaces.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Physical name of the backing vector-store collection. Globally unique so
    # two tenants can never be pointed at the same embedding space.
    vector_collection: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Denormalised counters, maintained by the ingestion pipeline. They back
    # the collection list screen, which would otherwise COUNT(*) over chunks
    # on every page load.
    document_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        # Partial, and that is the whole point: a plain unique constraint
        # would keep a soft-deleted collection's slug reserved forever, so
        # deleting "handbook" and creating it again would fail with a
        # conflict naming a row the user can no longer see. Scoped to live
        # rows, the slug is released by the delete and the uniqueness that
        # matters - among collections that exist - still holds.
        Index(
            "uq_collections_namespace_id_slug",
            "namespace_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Target for the composite foreign key on `documents`: it lets the
        # database guarantee that a document's namespace_id always equals its
        # collection's namespace_id.
        UniqueConstraint("id", "namespace_id", name="uq_collections_id_namespace_id"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint("document_count >= 0", name="document_count_non_negative"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
        CheckConstraint("embedding_dimensions > 0", name="embedding_dimensions_positive"),
    )

    namespace: Mapped[Namespace] = relationship(back_populates="collections")
    created_by: Mapped[User | None] = relationship()
    documents: Mapped[list[Document]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
