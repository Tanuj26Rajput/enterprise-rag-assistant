"""Authentication request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel

# Pydantic never serialises a field that is not declared, so the absence of
# `hashed_password` from UserRead is what keeps hashes out of responses.


class UserRead(ORMModel):
    """A user as the API exposes them. Contains no credential material."""

    id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    is_active: bool
    is_superuser: bool
    is_email_verified: bool
    last_login_at: datetime | None = None
    created_at: datetime


class RegisterRequest(BaseModel):
    email: EmailStr
    # Length is the dominant factor in password strength; composition rules
    # mostly push users toward predictable substitutions. The minimum is the
    # configured policy, enforced again in the service against settings.
    password: Annotated[str, Field(min_length=12, max_length=128)]
    full_name: Annotated[str | None, Field(default=None, max_length=255)]

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("password must not be blank")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=1)]


class TokenResponse(BaseModel):
    """An issued token pair.

    ``expires_at`` is absolute rather than a lifetime in seconds so a client
    with a skewed clock cannot keep using a token it believes is still fresh.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime
