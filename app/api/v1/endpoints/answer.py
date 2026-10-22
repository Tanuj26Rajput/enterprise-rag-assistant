"""Grounded answer generation.

POST, like search, and for the same reasons: questions can be long, and they
do not belong in access logs or browser history.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import AnswerServiceDep, CurrentUser
from app.schemas.answer import AnswerRequest, AnswerResponse

router = APIRouter(prefix="/namespaces/{namespace_id}/collections", tags=["answers"])


@router.post(
    "/{collection_id}/answer",
    response_model=AnswerResponse,
    summary="Answer a question from a collection's documents",
    description=(
        "Retrieves the most relevant passages the caller is authorized to see, then "
        "answers from those passages alone. Every claim carries a bracketed marker "
        "matching one of the returned citations. If the passages do not support an "
        "answer, `has_sufficient_evidence` is false and no citations are returned."
    ),
)
async def answer_question(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    payload: AnswerRequest,
    user: CurrentUser,
    service: AnswerServiceDep,
) -> AnswerResponse:
    result = await service.answer(
        user,
        namespace_id,
        collection_id,
        question=payload.question,
        top_k=payload.top_k,
        min_score=payload.min_score,
    )
    return AnswerResponse.from_result(payload.question, result)
