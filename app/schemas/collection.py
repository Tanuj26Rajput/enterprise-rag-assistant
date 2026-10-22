"""Collection schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.schemas.common import ORMModel
from app.schemas.namespace import SlugStr


class CollectionCreate(BaseModel):
    slug: SlugStr = Field(description="URL-safe identifier, unique within the namespace.")
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: Annotated[str | None, Field(default=None, max_length=4000)]
    # Omitted means "use the platform default". Pinning it per collection lets
    # one namespace migrate to a new embedding model without re-embedding the
    # rest, since a collection maps 1:1 to a vector-store collection.
    embedding_model: Annotated[str | None, Field(default=None, max_length=128)]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _normalise_slug(cls, value: str) -> str:
        return value.strip().lower()


class CollectionUpdate(BaseModel):
    """Partial update. Slug and embedding model are immutable, because both
    are baked into the vector collection the chunks already live in."""

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    description: Annotated[str | None, Field(default=None, max_length=4000)]
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class CollectionRead(ORMModel):
    id: uuid.UUID
    namespace_id: uuid.UUID
    slug: str
    name: str
    description: str | None
    vector_collection: str
    embedding_model: str
    embedding_dimensions: int
    is_active: bool
    document_count: int
    chunk_count: int
    created_by_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # SQLAlchemy reserves `metadata` on declarative classes, so the ORM
    # attribute is `meta` while the column and the API field are `metadata`.
    metadata: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("meta", "metadata")
    )
