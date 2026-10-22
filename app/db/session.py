"""Async engine, session factory and the FastAPI session dependency.

Schema management is NOT performed here. There is deliberately no
``Base.metadata.create_all()`` call anywhere in the application: the schema is
owned by Alembic, and the only way it changes is ``alembic upgrade``. Letting
the app create tables at startup would mean several workers racing to issue
DDL, and would let the running schema drift away from the migration history.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the async engine for the configured database."""
    settings = settings or get_settings()

    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "future": True,
        "pool_pre_ping": settings.db_pool_pre_ping,
        "connect_args": settings.db_connect_args,
    }

    if settings.uses_transaction_pooler:
        # Supavisor/PgBouncer in transaction mode already pools on the server
        # side. Pooling again in the client holds server connections open for
        # no benefit and breaks the pooler's fairness, so we hand every
        # checkout a fresh connection.
        kwargs["poolclass"] = NullPool
        logger.info("Transaction pooler detected; using NullPool and disabling prepared statements")
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            # Recycle below any idle timeout imposed by the provider, so we
            # never hand out a connection the server has already closed.
            pool_recycle=settings.db_pool_recycle,
        )

    return create_async_engine(settings.async_database_url, **kwargs)


engine: AsyncEngine = build_engine()

#: Session factory. ``expire_on_commit=False`` keeps ORM objects usable after
#: the request's transaction commits, which matters when a response model is
#: serialised after the dependency has exited.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to one request.

    The caller commits. Any exception escaping the handler rolls the
    transaction back, so a half-applied write can never be committed by a
    later request reusing the connection.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session for code outside the request cycle.

    Used by workers, CLI commands and tests, where there is no dependency
    injection to commit on our behalf.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_connection(timeout_hint: str = "SELECT 1") -> bool:
    """Liveness probe for the ``/health`` endpoint."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text(timeout_hint))
        return True
    except Exception:
        logger.exception("Database health check failed")
        return False


async def dispose_engine() -> None:
    """Close pooled connections on application shutdown."""
    await engine.dispose()
    logger.info("Database engine disposed")
