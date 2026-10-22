"""Request and response shapes for grounded answer generation."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.services.generation import Citation, GeneratedAnswer
from app.services.retrieval import MAX_QUERY_LENGTH


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Passages to retrieve as evidence. Defaults to RETRIEVAL_TOP_K.",
    )
    min_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Drop passages scoring below this before they reach the model.",
    )
    # Model, temperature and token budget are deliberately not accepted here.
    # They are deployment settings: letting a client pick the model turns an
    # authenticated endpoint into an open proxy to every model on the router,
    # billed to the operator.


class CitationRead(BaseModel):
    """One passage the answer relied on.

    Assembled from the stored chunk and its document, never from the model's
    prose, so every field here is checkable against the database.
    """

    marker: int = Field(description="The bracketed number used in the answer text.")
    document_id: uuid.UUID
    document_name: str
    chunk_id: uuid.UUID
    chunk_index: int
    excerpt: str = Field(description="The passage text the model was shown.")
    score: float
    page_number: int | None = Field(
        default=None, description="Null for formats that have no pages, never fabricated."
    )

    @classmethod
    def from_citation(cls, citation: Citation) -> CitationRead:
        return cls(
            marker=citation.marker,
            document_id=citation.document_id,
            document_name=citation.document_name,
            chunk_id=citation.chunk_id,
            chunk_index=citation.chunk_index,
            excerpt=citation.excerpt,
            score=citation.score,
            page_number=citation.page_number,
        )


class AnswerUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class AnswerResponse(BaseModel):
    question: str
    answer: str
    citations: list[CitationRead]
    has_sufficient_evidence: bool = Field(
        description="False when no passage supported an answer; the answer is then a refusal."
    )
    grounded: bool = Field(
        description="Whether at least one claim in the answer carries a resolvable citation."
    )
    model: str = Field(description="The model that actually served the request.")
    retrieved_count: int = Field(ge=0, description="Passages put in front of the model.")
    truncated: bool = Field(
        default=False, description="The model hit its token budget before finishing."
    )
    usage: AnswerUsage | None = None

    @classmethod
    def from_result(cls, question: str, result: GeneratedAnswer) -> AnswerResponse:
        usage = None
        if result.prompt_tokens is not None or result.completion_tokens is not None:
            total = None
            if result.prompt_tokens is not None and result.completion_tokens is not None:
                total = result.prompt_tokens + result.completion_tokens
            usage = AnswerUsage(
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=total,
            )
        return cls(
            question=question,
            answer=result.answer,
            citations=[CitationRead.from_citation(c) for c in result.citations],
            has_sufficient_evidence=result.has_sufficient_evidence,
            grounded=result.grounded,
            model=result.model,
            retrieved_count=result.retrieved_count,
            truncated=result.truncated,
            usage=usage,
        )
