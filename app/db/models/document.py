"""Documents and their extracted chunks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
from app.db.models.enums import DOCUMENT_STATUS_ENUM, DocumentStatus, pg_enum

if TYPE_CHECKING:
    from app.db.models.collection import Collection
    from app.db.models.namespace import Namespace
    from app.db.models.user import User


class Document(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """An uploaded source file and its ingestion state.

    The binary itself lives in object storage; this row is the durable record
    of its identity, lifecycle and provenance. ``status`` drives the ingestion
    state machine (pending -> processing -> ready | failed).
    """

    __tablename__ = "documents"

    # Denormalised from `collections` so that every tenant-scoped query and
    # index can filter on namespace_id without a join. The composite foreign
    # key below is what keeps the denormalisation honest.
    namespace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    collection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # SHA-256 of the raw bytes: the deduplication key and the integrity check
    # for re-processing.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    # Opaque pointer into object storage (e.g. s3://bucket/key).
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)

    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, DOCUMENT_STATUS_ENUM),
        nullable=False,
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    token_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
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
        ForeignKeyConstraint(
            ["collection_id", "namespace_id"],
            ["collections.id", "collections.namespace_id"],
            name="fk_documents_collection_id_namespace_id_collections",
            ondelete="CASCADE",
        ),
        # Deduplicate by content within a collection, but only among live
        # rows: re-uploading a file after a soft delete must succeed.
        Index(
            "uq_documents_collection_id_checksum_active",
            "collection_id",
            "checksum",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Document list screens: "newest documents in this namespace".
        Index(
            "ix_documents_namespace_id_created_at",
            "namespace_id",
            text("created_at DESC"),
        ),
        # Ingestion worker polling for work, and the failure dashboard.
        Index("ix_documents_status_created_at", "status", "created_at"),
        Index("ix_documents_collection_id", "collection_id"),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="page_count_positive"),
        CheckConstraint("char_length(checksum) = 64", name="checksum_is_sha256"),
        # A failed document must explain itself; a successful one must not
        # carry a stale error.
        CheckConstraint(
            "(status = 'failed') = (error_message IS NOT NULL)",
            name="error_message_matches_status",
        ),
    )

    # namespace_id is constrained through the composite key above rather than
    # by its own foreign key, so the join has to be spelled out. Read-only:
    # a document is re-homed by changing its collection, never its namespace.
    namespace: Mapped[Namespace] = relationship(
        back_populates="documents",
        primaryjoin="Document.namespace_id == Namespace.id",
        foreign_keys="Document.namespace_id",
        viewonly=True,
    )
    collection: Mapped[Collection] = relationship(back_populates="documents")
    uploaded_by: Mapped[User | None] = relationship(back_populates="uploaded_documents")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.chunk_index",
    )


class DocumentChunk(UUIDPrimaryKeyMixin, Base):
    """One retrievable passage of a document.

    The embedding vector lives in the vector store; this table holds the text
    and the positional metadata needed to render a citation. ``vector_id`` is
    the join key between the two stores, so a retrieval hit can be resolved
    back to an exact page and character range.

    No ``updated_at``: chunks are immutable. Re-processing a document deletes
    its chunks and writes new ones, which keeps citation ids honest.
    """

    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Identifier of this chunk's embedding in the vector store. Nullable
    # because the row is written before the embedding call succeeds.
    vector_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
        UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_document_chunks_document_id_chunk_index",
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("token_count > 0", name="token_count_positive"),
        CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end >= char_start",
            name="char_range_ordered",
        ),
        CheckConstraint("page_number IS NULL OR page_number > 0", name="page_number_positive"),
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
