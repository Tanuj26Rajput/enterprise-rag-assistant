"""Request and response shapes for conversations and chat."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.services.retrieval import MAX_QUERY_LENGTH


class ConversationCreate(BaseModel):
    collection_id: uuid.UUID = Field(description="The collection this thread retrieves from.")
    title: str | None = Field(default=None, max_length=255)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ConversationRead(ORMModel):
    id: uuid.UUID
    namespace_id: uuid.UUID
    title: str | None
    model: str
    collection_ids: list[str]
    message_count: int
    total_tokens: int
    is_archived: bool
    created_at: datetime
    last_message_at: datetime | None = None
    # user_id is deliberately absent: a thread is only ever returned to the
    # user who owns it, so echoing the owner adds nothing and would make a
    # leak through another route harder to notice.


class MessageRead(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sequence: int
    role: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    error: str | None = None
    created_at: datetime


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    collection_id: uuid.UUID | None = Field(
        default=None,
        description="Overrides the conversation's collection for this turn.",
    )
    top_k: int | None = Field(default=None, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=-1.0, le=1.0)
