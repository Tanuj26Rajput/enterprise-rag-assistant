"""Namespace and membership schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.db.models.enums import NamespaceRole
from app.schemas.common import ORMModel

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$"
SlugStr = Annotated[str, Field(pattern=SLUG_PATTERN, max_length=63)]


class NamespaceCreate(BaseModel):
    slug: SlugStr = Field(description="URL-safe identifier, unique across the platform.")
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: Annotated[str | None, Field(default=None, max_length=4000)]
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _normalise_slug(cls, value: str) -> str:
        return value.strip().lower()


class NamespaceUpdate(BaseModel):
    """Partial update. The slug is immutable: it appears in URLs and in the
    names of vector-store collections, so changing it would orphan them."""

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    description: Annotated[str | None, Field(default=None, max_length=4000)]
    is_active: bool | None = None
    settings: dict[str, Any] | None = None


class NamespaceRead(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    owner_id: uuid.UUID
    is_active: bool
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class NamespaceWithRole(NamespaceRead):
    """A namespace plus the caller's own role in it."""

    role: NamespaceRole


class MemberCreate(BaseModel):
    email: EmailStr
    role: NamespaceRole = NamespaceRole.MEMBER

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class MemberUpdate(BaseModel):
    role: NamespaceRole


class MemberRead(ORMModel):
    id: uuid.UUID
    namespace_id: uuid.UUID
    user_id: uuid.UUID
    role: NamespaceRole
    invited_by_id: uuid.UUID | None
    created_at: datetime
