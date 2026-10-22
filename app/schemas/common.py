"""Shared request and response shapes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for responses built from ORM instances."""

    model_config = ConfigDict(from_attributes=True)


class PageParams(BaseModel):
    """Offset pagination.

    Offsets are used rather than cursors because the list endpoints are
    ordered by a mutable column (``created_at``) and the result sets are small
    and tenant-scoped. ``limit`` is capped so a client cannot ask for a
    namespace's entire document history in one request.
    """

    limit: Annotated[int, Query(ge=1, le=100)] = 20
    offset: Annotated[int, Query(ge=0)] = 0


class Page[T](BaseModel):
    """A single page of results plus the total matching count."""

    items: list[T]
    total: int = Field(ge=0, description="Total rows matching the query, ignoring pagination.")
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> Page[T]:
        return cls(items=items, total=total, limit=params.limit, offset=params.offset)


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable, machine-readable error identifier.")
    message: str = Field(description="Human-readable explanation.")
    details: Any | None = Field(default=None, description="Field-level errors, when applicable.")
    request_id: str | None = Field(default=None, description="Correlates with the server logs.")


class ErrorResponse(BaseModel):
    """The response body for every non-2xx response."""

    error: ErrorDetail
