"""Request and response shapes for retrieval."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.services.retrieval import MAX_QUERY_LENGTH, RetrievedChunk


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    top_k: int | None = Field(default=None, ge=1, le=50)
    min_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Drop hits scoring below this. Cosine similarity: 1.0 is identical.",
    )


class SearchHit(ORMModel):
    """One retrieved passage with everything a citation needs."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    content: str
    score: float
    page_number: int | None = None
    char_start: int | None = None
    char_end: int | None = None

    @classmethod
    def from_result(cls, result: RetrievedChunk) -> SearchHit:
        chunk, document = result.chunk, result.document
        return cls(
            chunk_id=chunk.id,
            document_id=document.id,
            # Falls back to the filename: a citation with no label is not
            # actionable for the reader.
            document_title=document.title or document.filename,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            score=result.score,
            page_number=chunk.page_number,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        )


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
