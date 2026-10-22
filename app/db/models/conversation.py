"""Conversations and their messages (the conversational-memory store)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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
from app.db.models.enums import MESSAGE_ROLE_ENUM, MessageRole, pg_enum

if TYPE_CHECKING:
    from app.db.models.namespace import Namespace
    from app.db.models.user import User


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A chat thread scoped to one namespace and owned by one user.

    ``collection_ids`` records which collections the thread may retrieve from.
    It is stored as JSONB rather than a join table because it is always read
    and written whole, as part of loading the conversation, and is never
    queried across conversations.
    """

    __tablename__ = "conversations"

    namespace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("namespaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Nullable: titles are generated from the first exchange.
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Denormalised so the conversation list can sort and paginate without
    # touching the (much larger) messages table.
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    collection_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        # The conversation sidebar: this user's threads in this namespace,
        # most recent first.
        Index(
            "ix_conversations_user_id_namespace_id_last_message_at",
            "user_id",
            "namespace_id",
            text("last_message_at DESC"),
        ),
        Index("ix_conversations_namespace_id", "namespace_id"),
        CheckConstraint("message_count >= 0", name="message_count_non_negative"),
        CheckConstraint("total_tokens >= 0", name="total_tokens_non_negative"),
        CheckConstraint("jsonb_typeof(collection_ids) = 'array'", name="collection_ids_is_array"),
    )

    namespace: Mapped[Namespace] = relationship(back_populates="conversations")
    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.sequence",
    )


class Message(UUIDPrimaryKeyMixin, Base):
    """One turn in a conversation.

    Messages are append-only, so there is no ``updated_at``. ``sequence`` gives
    a stable total order within a thread: timestamps alone are not enough,
    because a user message and its streamed reply can land in the same
    millisecond.

    ``citations`` holds the resolved sources for an assistant turn as a JSONB
    array of ``{chunk_id, document_id, page, score, snippet}``. It is a
    snapshot on purpose - a citation must still render correctly after the
    underlying document has been re-processed or deleted.
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[MessageRole] = mapped_column(
        pg_enum(MessageRole, MESSAGE_ROLE_ENUM), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    # Per-turn generation telemetry, for cost reporting and latency SLOs.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

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
            "conversation_id", "sequence", name="uq_messages_conversation_id_sequence"
        ),
        # Loading a thread, and paging back through its history.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="prompt_tokens_non_negative",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="completion_tokens_non_negative",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_ms_non_negative"),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="citations_is_array"),
        # Only assistant turns may carry citations.
        CheckConstraint(
            "role = 'assistant' OR citations = '[]'::jsonb",
            name="citations_only_on_assistant_turns",
        ),
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
