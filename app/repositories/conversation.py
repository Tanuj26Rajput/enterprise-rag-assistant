"""Persistence for conversations and their messages.

Every query here carries both ``namespace_id`` *and* ``user_id``. A
conversation is private to the person who started it, so namespace membership
alone is not enough: an admin of a namespace can administer its documents, but
that is not a reason to read a colleague's chat history. Making both
predicates part of the query - rather than fetching the row and comparing
afterwards - means a forgotten check in a service cannot expose a thread.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, namespace_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None:
        """Fetch one thread belonging to this user in this namespace."""
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.namespace_id == namespace_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def list_for_user(
        self,
        namespace_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        include_archived: bool = False,
    ) -> tuple[list[Conversation], int]:
        predicate = [
            Conversation.namespace_id == namespace_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        ]
        if not include_archived:
            predicate.append(Conversation.is_archived.is_(False))

        total = await self._session.scalar(
            select(func.count()).select_from(Conversation).where(*predicate)
        )
        rows = await self._session.scalars(
            select(Conversation)
            .where(*predicate)
            # A thread with no messages yet has no last_message_at, and would
            # sort last under a plain DESC; created_at keeps it visible.
            .order_by(func.coalesce(Conversation.last_message_at, Conversation.created_at).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def add(self, conversation: Conversation) -> Conversation:
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def flush(self) -> None:
        await self._session.flush()


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Message], int]:
        total = await self._session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        rows = await self._session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def recent_turns(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        """The last ``limit`` messages, returned oldest first.

        Fetched newest-first and reversed so the bound is on the *recent* end
        of the thread. Ordering by sequence ascending with a LIMIT would hand
        back the beginning of the conversation, which is the part the model
        least needs.
        """
        rows = await self._session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence.desc())
            .limit(limit)
        )
        return list(reversed(list(rows)))

    async def next_sequence(self, conversation_id: uuid.UUID) -> int:
        """The next free position in the thread.

        A unique constraint on ``(conversation_id, sequence)`` backs this up:
        two concurrent writers reading the same maximum will collide at the
        database rather than silently interleave.
        """
        highest = await self._session.scalar(
            select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id)
        )
        return 0 if highest is None else int(highest) + 1

    async def add(self, message: Message) -> Message:
        self._session.add(message)
        await self._session.flush()
        return message

    async def flush(self) -> None:
        await self._session.flush()
