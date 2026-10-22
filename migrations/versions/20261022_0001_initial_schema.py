"""Initial schema: users, namespaces, collections, documents, chat, audit.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-22

Enum types are created with explicit DDL rather than being emitted implicitly
by ``create_table``. It costs a few lines but makes the migration idempotent,
renders correctly in ``--sql`` (offline) mode, and gives ``downgrade()``
something precise to drop - PostgreSQL does not remove an enum type when the
last table using it goes away.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "namespace_role": ("owner", "admin", "member", "viewer"),
    "document_status": ("pending", "processing", "ready", "failed", "archived"),
    "message_role": ("system", "user", "assistant", "tool"),
    "audit_action": (
        "create",
        "read",
        "update",
        "delete",
        "login",
        "logout",
        "query",
        "export",
    ),
    "audit_outcome": ("success", "failure", "denied"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum type without re-emitting CREATE TYPE."""
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    for name, values in ENUM_TYPES.items():
        rendered = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    # ------------------------------------------------------------- users ---
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "is_superuser",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_email_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_is_lowercase")),
        sa.CheckConstraint(
            "position('@' in email) > 1", name=op.f("ck_users_email_has_local_part")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"])

    # -------------------------------------------------------- namespaces ---
    op.create_table(
        "namespaces",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "settings",
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$'", name=op.f("ck_namespaces_slug_format")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_namespaces_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_namespaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_namespaces_slug")),
    )
    op.create_index("ix_namespaces_created_at", "namespaces", ["created_at"])
    op.create_index("ix_namespaces_deleted_at", "namespaces", ["deleted_at"])
    op.create_index("ix_namespaces_owner_id", "namespaces", ["owner_id"])

    # -------------------------------------------------- namespace_members ---
    op.create_table(
        "namespace_members",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("namespace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", _enum("namespace_role"), server_default="member", nullable=False),
        sa.Column("invited_by_id", sa.Uuid(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            name=op.f("fk_namespace_members_invited_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespaces.id"],
            name=op.f("fk_namespace_members_namespace_id_namespaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_namespace_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_namespace_members")),
        sa.UniqueConstraint(
            "namespace_id",
            "user_id",
            name=op.f("uq_namespace_members_namespace_id_user_id"),
        ),
    )
    op.create_index("ix_namespace_members_created_at", "namespace_members", ["created_at"])
    op.create_index(
        "ix_namespace_members_namespace_id_role",
        "namespace_members",
        ["namespace_id", "role"],
    )
    op.create_index("ix_namespace_members_user_id", "namespace_members", ["user_id"])

    # ------------------------------------------------------- collections ---
    op.create_table(
        "collections",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("namespace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("vector_collection", sa.String(length=128), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("document_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "metadata",
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "chunk_count >= 0", name=op.f("ck_collections_chunk_count_non_negative")
        ),
        sa.CheckConstraint(
            "document_count >= 0",
            name=op.f("ck_collections_document_count_non_negative"),
        ),
        sa.CheckConstraint(
            "embedding_dimensions > 0",
            name=op.f("ck_collections_embedding_dimensions_positive"),
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$'",
            name=op.f("ck_collections_slug_format"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_collections_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespaces.id"],
            name=op.f("fk_collections_namespace_id_namespaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
        sa.UniqueConstraint("id", "namespace_id", name=op.f("uq_collections_id_namespace_id")),
        sa.UniqueConstraint("namespace_id", "slug", name=op.f("uq_collections_namespace_id_slug")),
        sa.UniqueConstraint("vector_collection", name=op.f("uq_collections_vector_collection")),
    )
    op.create_index("ix_collections_created_at", "collections", ["created_at"])
    op.create_index("ix_collections_deleted_at", "collections", ["deleted_at"])

    # --------------------------------------------------------- documents ---
    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("namespace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("collection_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("storage_uri", sa.String(length=1024), nullable=False),
        sa.Column("status", _enum("document_status"), server_default="pending", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "metadata",
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(checksum) = 64", name=op.f("ck_documents_checksum_is_sha256")
        ),
        sa.CheckConstraint("chunk_count >= 0", name=op.f("ck_documents_chunk_count_non_negative")),
        sa.CheckConstraint(
            "(status = 'failed') = (error_message IS NOT NULL)",
            name=op.f("ck_documents_error_message_matches_status"),
        ),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count > 0",
            name=op.f("ck_documents_page_count_positive"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_documents_size_bytes_positive")),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_documents_token_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["collection_id", "namespace_id"],
            ["collections.id", "collections.namespace_id"],
            name=op.f("fk_documents_collection_id_namespace_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_documents_uploaded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index("ix_documents_collection_id", "documents", ["collection_id"])
    op.create_index("ix_documents_created_at", "documents", ["created_at"])
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])
    op.create_index(
        "ix_documents_namespace_id_created_at",
        "documents",
        ["namespace_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_documents_status_created_at", "documents", ["status", "created_at"])
    op.create_index(
        "uq_documents_collection_id_checksum_active",
        "documents",
        ["collection_id", "checksum"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --------------------------------------------------- document_chunks ---
    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_title", sa.String(length=512), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("vector_id", sa.String(length=128), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
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
        sa.CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end >= char_start",
            name=op.f("ck_document_chunks_char_range_ordered"),
        ),
        sa.CheckConstraint(
            "chunk_index >= 0", name=op.f("ck_document_chunks_chunk_index_non_negative")
        ),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number > 0",
            name=op.f("ck_document_chunks_page_number_positive"),
        ),
        sa.CheckConstraint("token_count > 0", name=op.f("ck_document_chunks_token_count_positive")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name=op.f("uq_document_chunks_document_id_chunk_index"),
        ),
        sa.UniqueConstraint("vector_id", name=op.f("uq_document_chunks_vector_id")),
    )

    # ----------------------------------------------------- conversations ---
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("namespace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "collection_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "settings",
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(collection_ids) = 'array'",
            name=op.f("ck_conversations_collection_ids_is_array"),
        ),
        sa.CheckConstraint(
            "message_count >= 0",
            name=op.f("ck_conversations_message_count_non_negative"),
        ),
        sa.CheckConstraint(
            "total_tokens >= 0", name=op.f("ck_conversations_total_tokens_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespaces.id"],
            name=op.f("fk_conversations_namespace_id_namespaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index("ix_conversations_created_at", "conversations", ["created_at"])
    op.create_index("ix_conversations_deleted_at", "conversations", ["deleted_at"])
    op.create_index("ix_conversations_namespace_id", "conversations", ["namespace_id"])
    op.create_index(
        "ix_conversations_user_id_namespace_id_last_message_at",
        "conversations",
        ["user_id", "namespace_id", sa.text("last_message_at DESC")],
    )

    # ---------------------------------------------------------- messages ---
    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", _enum("message_role"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
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
        sa.CheckConstraint(
            "jsonb_typeof(citations) = 'array'",
            name=op.f("ck_messages_citations_is_array"),
        ),
        sa.CheckConstraint(
            "role = 'assistant' OR citations = '[]'::jsonb",
            name=op.f("ck_messages_citations_only_on_assistant_turns"),
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name=op.f("ck_messages_completion_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_messages_latency_ms_non_negative"),
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name=op.f("ck_messages_prompt_tokens_non_negative"),
        ),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_messages_sequence_non_negative")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence",
            name=op.f("uq_messages_conversation_id_sequence"),
        ),
    )
    op.create_index(
        "ix_messages_conversation_id_created_at",
        "messages",
        ["conversation_id", "created_at"],
    )

    # -------------------------------------------------------- audit_logs ---
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=True),
        sa.Column("namespace_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", _enum("audit_action"), nullable=False),
        sa.Column("event", sa.String(length=128), nullable=False),
        sa.Column("outcome", _enum("audit_outcome"), server_default="success", nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("resource_label", sa.String(length=512), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("http_method", sa.String(length=10), nullable=True),
        sa.Column("http_path", sa.String(length=512), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
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
        sa.CheckConstraint(
            "http_status IS NULL OR (http_status BETWEEN 100 AND 599)",
            name=op.f("ck_audit_logs_http_status_in_range"),
        ),
        sa.CheckConstraint(
            "(resource_id IS NULL) OR (resource_type IS NOT NULL)",
            name=op.f("ck_audit_logs_resource_id_requires_type"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_logs_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespaces.id"],
            name=op.f("fk_audit_logs_namespace_id_namespaces"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_actor_id_created_at",
        "audit_logs",
        ["actor_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_audit_logs_event_created_at",
        "audit_logs",
        ["event", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_audit_logs_namespace_id_created_at",
        "audit_logs",
        ["namespace_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])
    op.create_index(
        "ix_audit_logs_resource_type_resource_id",
        "audit_logs",
        ["resource_type", "resource_id"],
    )


def downgrade() -> None:
    # Reverse creation order so dependent tables go first.
    op.drop_table("audit_logs")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("collections")
    op.drop_table("namespace_members")
    op.drop_table("namespaces")
    op.drop_table("users")

    # Dropping the tables leaves the enum types behind; remove them explicitly
    # so the downgrade is a true inverse of the upgrade.
    for name in reversed(list(ENUM_TYPES)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
