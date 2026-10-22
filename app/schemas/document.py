"""Document schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from app.db.models.enums import DocumentStatus, JobKind, JobStatus
from app.schemas.common import ORMModel


class DocumentRead(ORMModel):
    """A document's durable record.

    ``storage_uri`` is deliberately absent: it points at internal object
    storage and is of no use to a client.
    """

    id: uuid.UUID
    namespace_id: uuid.UUID
    collection_id: uuid.UUID
    filename: str
    title: str | None
    mime_type: str
    size_bytes: int
    checksum: str
    status: DocumentStatus
    error_message: str | None
    page_count: int | None
    chunk_count: int
    token_count: int
    processing_started_at: datetime | None
    processed_at: datetime | None
    uploaded_by_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("meta", "metadata")
    )


class DocumentChunkRead(ORMModel):
    """A chunk, as returned when inspecting a document or rendering a citation."""

    id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    page_number: int | None
    section_title: str | None
    char_start: int | None
    char_end: int | None
    vector_id: str | None
    created_at: datetime


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] | None = None


class IngestionJobRead(ORMModel):
    """One unit of durable work against a document.

    Exposed so a client can distinguish the three states a non-ready document
    can be in - waiting, being retried, or given up on - which the document's
    own status deliberately compresses. ``lease_owner`` is withheld: it names
    an internal worker process and is operator-facing, not user-facing.
    """

    id: uuid.UUID
    document_id: uuid.UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ReconcileResult(BaseModel):
    """The outcome of a stale vector sweep."""

    collection_id: uuid.UUID
    vectors_removed: int
