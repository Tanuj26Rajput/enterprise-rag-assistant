"""Retrieval endpoint.

POST rather than GET: a query can be long enough to be awkward in a URL, and
keeping user questions out of access logs and browser history is worth more
than cacheability on a route whose results change with every upload.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, RetrievalServiceDep
from app.schemas.search import SearchHit, SearchRequest, SearchResponse

router = APIRouter(prefix="/namespaces/{namespace_id}/collections", tags=["search"])


@router.post(
    "/{collection_id}/search",
    response_model=SearchResponse,
    summary="Search a collection",
)
async def search_collection(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    payload: SearchRequest,
    user: CurrentUser,
    service: RetrievalServiceDep,
) -> SearchResponse:
    results = await service.search(
        user,
        namespace_id,
        collection_id,
        query=payload.query,
        top_k=payload.top_k,
        min_score=payload.min_score,
    )
    return SearchResponse(
        query=payload.query,
        hits=[SearchHit.from_result(result) for result in results],
    )
