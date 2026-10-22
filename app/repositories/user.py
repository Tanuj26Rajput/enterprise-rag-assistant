"""Persistence for users.

Repositories are the only place that builds SQL. Services depend on these
classes rather than on ``AsyncSession`` directly, which keeps authorisation
logic testable without a database and leaves one obvious place to add
tenant-scoping to a query.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        # Emails are stored lower-cased (enforced by a check constraint), so
        # a plain equality match is already case-insensitive.
        stmt = select(User).where(User.email == email.strip().lower())
        return await self._session.scalar(stmt)

    async def exists_with_email(self, email: str) -> bool:
        stmt = select(func.count()).select_from(User).where(User.email == email.strip().lower())
        return bool(await self._session.scalar(stmt))

    async def add(self, user: User) -> User:
        self._session.add(user)
        # Flush rather than commit: the caller owns the transaction, and the
        # server-generated id is needed before it ends.
        await self._session.flush()
        return user
