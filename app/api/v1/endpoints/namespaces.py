"""Namespace and membership endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, NamespaceServiceDep, PageParamsDep, SessionDep
from app.db.models.enums import NamespaceRole
from app.schemas.common import Page
from app.schemas.namespace import (
    MemberCreate,
    MemberRead,
    MemberUpdate,
    NamespaceCreate,
    NamespaceRead,
    NamespaceUpdate,
    NamespaceWithRole,
)

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


def _with_role(namespace: object, role: NamespaceRole) -> NamespaceWithRole:
    """Attach the caller's own role to a namespace representation."""
    return NamespaceWithRole(**NamespaceRead.model_validate(namespace).model_dump(), role=role)


@router.post(
    "",
    response_model=NamespaceWithRole,
    status_code=status.HTTP_201_CREATED,
    summary="Create a namespace",
)
async def create_namespace(
    payload: NamespaceCreate,
    user: CurrentUser,
    service: NamespaceServiceDep,
    session: SessionDep,
) -> NamespaceWithRole:
    namespace, role = await service.create(user, payload)
    await session.commit()
    return _with_role(namespace, role)


@router.get("", response_model=Page[NamespaceWithRole], summary="List your namespaces")
async def list_namespaces(
    user: CurrentUser,
    service: NamespaceServiceDep,
    page: PageParamsDep,
) -> Page[NamespaceWithRole]:
    rows, total = await service.list_for_user(user, limit=page.limit, offset=page.offset)
    items = [_with_role(namespace, role) for namespace, role in rows]
    return Page.build(items, total, page)


@router.get("/{namespace_id}", response_model=NamespaceWithRole, summary="Read a namespace")
async def read_namespace(
    namespace_id: uuid.UUID,
    user: CurrentUser,
    service: NamespaceServiceDep,
) -> NamespaceWithRole:
    context = await service.get(user, namespace_id)
    return _with_role(context.namespace, context.role)


@router.patch("/{namespace_id}", response_model=NamespaceRead, summary="Update a namespace")
async def update_namespace(
    namespace_id: uuid.UUID,
    payload: NamespaceUpdate,
    user: CurrentUser,
    service: NamespaceServiceDep,
    session: SessionDep,
) -> NamespaceRead:
    context = await service.update(user, namespace_id, payload)
    await session.commit()
    return NamespaceRead.model_validate(context.namespace)


@router.delete(
    "/{namespace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a namespace (owner only)",
)
async def delete_namespace(
    namespace_id: uuid.UUID,
    user: CurrentUser,
    service: NamespaceServiceDep,
    session: SessionDep,
) -> None:
    await service.delete(user, namespace_id)
    await session.commit()


# ------------------------------------------------------------- membership ---
@router.get("/{namespace_id}/members", response_model=Page[MemberRead], summary="List members")
async def list_members(
    namespace_id: uuid.UUID,
    user: CurrentUser,
    service: NamespaceServiceDep,
    page: PageParamsDep,
) -> Page[MemberRead]:
    members, total = await service.list_members(
        user, namespace_id, limit=page.limit, offset=page.offset
    )
    return Page.build([MemberRead.model_validate(m) for m in members], total, page)


@router.post(
    "/{namespace_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member (admin or owner)",
)
async def add_member(
    namespace_id: uuid.UUID,
    payload: MemberCreate,
    user: CurrentUser,
    service: NamespaceServiceDep,
    session: SessionDep,
) -> MemberRead:
    member = await service.add_member(user, namespace_id, email=payload.email, role=payload.role)
    await session.commit()
    return MemberRead.model_validate(member)


@router.patch(
    "/{namespace_id}/members/{member_id}",
    response_model=MemberRead,
    summary="Change a member's role",
)
async def update_member(
    namespace_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: MemberUpdate,
    user: CurrentUser,
    service: NamespaceServiceDep,
    session: SessionDep,
) -> MemberRead:
    member = await service.update_member_role(user, namespace_id, member_id, payload.role)
    await session.commit()
    return MemberRead.model_validate(member)


@router.delete(
    "/{namespace_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
)
async def remove_member(
    namespace_id: uuid.UUID,
    member_id: uuid.UUID,
    user: CurrentUser,
    service: NamespaceServiceDep,
    session: SessionDep,
) -> None:
    await service.remove_member(user, namespace_id, member_id)
    await session.commit()
