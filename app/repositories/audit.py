"""Persistence for the audit trail."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditAction, AuditLog, AuditOutcome


class AuditRepository:
    """Writes audit entries.

    Entries are written in the same transaction as the operation they
    describe, so the trail cannot disagree with the data: if the operation
    rolls back, so does its audit row.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        action: AuditAction,
        event: str,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        actor_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        namespace_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        resource_label: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        http_method: str | None = None,
        http_path: str | None = None,
        http_status: int | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            event=event,
            outcome=outcome,
            actor_id=actor_id,
            actor_email=actor_email,
            namespace_id=namespace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_label=resource_label,
            ip_address=ip_address,
            user_agent=user_agent[:512] if user_agent else None,
            request_id=request_id,
            http_method=http_method,
            http_path=http_path,
            http_status=http_status,
            error_message=error_message,
            meta=metadata or {},
        )
        self._session.add(entry)
        await self._session.flush()
        return entry
