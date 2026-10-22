"""Persistence for collections."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Collection


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, namespace_id: uuid.UUID, collection_id: uuid.UUID) -> Collection | None:
        """Fetch a collection *within* a namespace.

        The namespace predicate is not an optimisation: it is what makes a
        collection id from another tenant resolve to nothing.
        """
        stmt = select(Collection).where(
            Collection.id == collection_id,
            Collection.namespace_id == namespace_id,
            Collection.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def get_any(self, collection_id: uuid.UUID) -> Collection | None:
        """Fetch by id alone, for background jobs. See the note on
        :meth:`DocumentRepository.get_any`: no tenant predicate, so it must
        never serve a request on a user's behalf.
        """
        return await self._session.scalar(select(Collection).where(Collection.id == collection_id))

    async def get_by_slug(self, namespace_id: uuid.UUID, slug: str) -> Collection | None:
        stmt = select(Collection).where(
            Collection.namespace_id == namespace_id,
            Collection.slug == slug.strip().lower(),
            Collection.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def list_for_namespace(
        self, namespace_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Collection], int]:
        predicate = (Collection.namespace_id == namespace_id, Collection.deleted_at.is_(None))
        total = await self._session.scalar(
            select(func.count()).select_from(Collection).where(*predicate)
        )
        rows = await self._session.scalars(
            select(Collection)
            .where(*predicate)
            .order_by(Collection.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def add(self, collection: Collection) -> Collection:
        self._session.add(collection)
        await self._session.flush()
        return collection

    async def flush(self) -> None:
        await self._session.flush()
