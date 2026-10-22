"""Version 1 API surface."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    answer,
    auth,
    collections,
    conversations,
    documents,
    namespaces,
    search,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(namespaces.router)
api_router.include_router(collections.router)
api_router.include_router(documents.router)
api_router.include_router(search.router)
api_router.include_router(answer.router)
api_router.include_router(conversations.router)
