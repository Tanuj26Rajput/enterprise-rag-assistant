"""Persistence for namespaces and their membership."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Namespace, NamespaceMember, NamespaceRole, User


class NamespaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, namespace_id: uuid.UUID) -> Namespace | None:
        """Fetch a live namespace. Soft-deleted rows are invisible."""
        stmt = select(Namespace).where(
            Namespace.id == namespace_id,
            Namespace.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def get_by_slug(self, slug: str) -> Namespace | None:
        stmt = select(Namespace).where(
            Namespace.slug == slug.strip().lower(),
            Namespace.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[tuple[Namespace, NamespaceRole]], int]:
        """List the namespaces this user belongs to, with their role.

        The join against ``namespace_members`` is the tenancy filter: a user
        who is not a member simply has no row, so another tenant's namespaces
        can never appear in the result.
        """
        base = (
            select(Namespace, NamespaceMember.role)
            .join(NamespaceMember, NamespaceMember.namespace_id == Namespace.id)
            .where(NamespaceMember.user_id == user_id, Namespace.deleted_at.is_(None))
        )
        total = await self._session.scalar(
            select(func.count())
            .select_from(NamespaceMember)
            .join(Namespace, Namespace.id == NamespaceMember.namespace_id)
            .where(NamespaceMember.user_id == user_id, Namespace.deleted_at.is_(None))
        )
        rows = await self._session.execute(
            base.order_by(Namespace.created_at.desc()).limit(limit).offset(offset)
        )
        return [(row[0], row[1]) for row in rows.all()], int(total or 0)

    async def add(self, namespace: Namespace) -> Namespace:
        self._session.add(namespace)
        await self._session.flush()
        return namespace

    async def soft_delete(self, namespace: Namespace, *, at: object) -> None:
        namespace.deleted_at = at  # type: ignore[assignment]
        await self._session.flush()


class NamespaceMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, namespace_id: uuid.UUID, user_id: uuid.UUID) -> NamespaceMember | None:
        stmt = select(NamespaceMember).where(
            NamespaceMember.namespace_id == namespace_id,
            NamespaceMember.user_id == user_id,
        )
        return await self._session.scalar(stmt)

    async def get_by_id(
        self, namespace_id: uuid.UUID, member_id: uuid.UUID
    ) -> NamespaceMember | None:
        # Scoped by namespace_id as well as the primary key, so a member id
        # from one namespace cannot be used against another.
        stmt = select(NamespaceMember).where(
            NamespaceMember.id == member_id,
            NamespaceMember.namespace_id == namespace_id,
        )
        return await self._session.scalar(stmt)

    async def list_for_namespace(
        self, namespace_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[NamespaceMember], int]:
        total = await self._session.scalar(
            select(func.count())
            .select_from(NamespaceMember)
            .where(NamespaceMember.namespace_id == namespace_id)
        )
        rows = await self._session.scalars(
            select(NamespaceMember)
            .where(NamespaceMember.namespace_id == namespace_id)
            .order_by(NamespaceMember.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def count_owners(self, namespace_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(NamespaceMember)
            .where(
                NamespaceMember.namespace_id == namespace_id,
                NamespaceMember.role == NamespaceRole.OWNER,
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    async def add(self, member: NamespaceMember) -> NamespaceMember:
        self._session.add(member)
        await self._session.flush()
        return member

    async def remove(self, member: NamespaceMember) -> None:
        await self._session.delete(member)
        await self._session.flush()

    async def find_user_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.strip().lower())
        return await self._session.scalar(stmt)
