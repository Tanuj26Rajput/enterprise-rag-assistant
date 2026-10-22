"""Collection endpoints, nested under their namespace.

Nesting is deliberate: the namespace is part of every path, so a request can
never name a collection without also naming the tenant it is claimed to be in,
and the service checks the two agree.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import CollectionServiceDep, CurrentUser, PageParamsDep, SessionDep
from app.schemas.collection import CollectionCreate, CollectionRead, CollectionUpdate
from app.schemas.common import Page

router = APIRouter(prefix="/namespaces/{namespace_id}/collections", tags=["collections"])


@router.post(
    "",
    response_model=CollectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a collection",
)
async def create_collection(
    namespace_id: uuid.UUID,
    payload: CollectionCreate,
    user: CurrentUser,
    service: CollectionServiceDep,
    session: SessionDep,
) -> CollectionRead:
    collection = await service.create(user, namespace_id, payload)
    await session.commit()
    return CollectionRead.model_validate(collection)


@router.get("", response_model=Page[CollectionRead], summary="List collections")
async def list_collections(
    namespace_id: uuid.UUID,
    user: CurrentUser,
    service: CollectionServiceDep,
    page: PageParamsDep,
) -> Page[CollectionRead]:
    collections, total = await service.list_for_namespace(
        user, namespace_id, limit=page.limit, offset=page.offset
    )
    return Page.build([CollectionRead.model_validate(c) for c in collections], total, page)


@router.get("/{collection_id}", response_model=CollectionRead, summary="Read a collection")
async def read_collection(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    user: CurrentUser,
    service: CollectionServiceDep,
) -> CollectionRead:
    collection = await service.get(user, namespace_id, collection_id)
    return CollectionRead.model_validate(collection)


@router.patch("/{collection_id}", response_model=CollectionRead, summary="Update a collection")
async def update_collection(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
    user: CurrentUser,
    service: CollectionServiceDep,
    session: SessionDep,
) -> CollectionRead:
    collection = await service.update(user, namespace_id, collection_id, payload)
    await session.commit()
    return CollectionRead.model_validate(collection)


@router.delete(
    "/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a collection (admin or owner)",
)
async def delete_collection(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    user: CurrentUser,
    service: CollectionServiceDep,
    session: SessionDep,
) -> None:
    await service.delete(user, namespace_id, collection_id)
    await session.commit()
