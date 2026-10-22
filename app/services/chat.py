"""Conversations, and streamed answers within them.

**Ownership.** A conversation belongs to one user inside one namespace, and
both predicates are part of every query (see
:mod:`app.repositories.conversation`). Namespace membership is necessary but
not sufficient: administering a namespace's documents is not a reason to read
a colleague's chat history, so even an owner cannot open someone else's
thread. A thread belonging to another user is reported as missing rather than
forbidden, for the same anti-enumeration reason as every other resource here.

**Bounded history.** Only the tail of a thread is replayed, bounded by both a
message count and a character budget. An unbounded history would grow the
prompt until the request failed - and would do so gradually, so the first
symptom would be rising cost rather than an error.

**Persistence around a stream.** The user's turn is written before the first
token is requested, so a question is never lost to a failure mid-answer. The
assistant's turn is written when the stream ends - completed, failed upstream,
or abandoned - and a partial answer is stored as a partial answer rather than
discarded, with the reason recorded on the row. That last write is best
effort: if the server task is cancelled outright rather than unwound, it may
not run. The question is already durable by then, so the worst case is a
turn that is missing its partial answer, never a lost question.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import NotFound, ServiceUnavailable, ValidationFailed
from app.db.models import (
    AuditAction,
    Conversation,
    Message,
    MessageRole,
    NamespaceRole,
    User,
)
from app.llm.base import ChatMessage, ChatProvider, LLMError
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.repositories.conversation import ConversationRepository, MessageRepository
from app.services.authorization import NamespaceAuthorizer
from app.services.generation import (
    AnswerContext,
    AnswerService,
    Citation,
    translate_llm_error,
)
from app.services.retrieval import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

#: Why a stream ended, recorded on the assistant message.
FINISH_CLIENT_DISCONNECT = "client_disconnect"
FINISH_UPSTREAM_ERROR = "error"


@dataclass(slots=True)
class StreamOutcome:
    """What a stream produced, filled in as it runs.

    Mutable and owned by the caller so that the endpoint can persist whatever
    was produced even when the stream is abandoned part way through.
    """

    conversation_id: uuid.UUID
    question: str
    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    model: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    refused: bool = False
    retrieved_count: int = 0
    error: str | None = None
    persisted: bool = False

    @property
    def grounded(self) -> bool:
        return bool(self.citations)


def citation_payload(citation: Citation) -> dict[str, object]:
    """The JSONB shape stored on an assistant message.

    A snapshot, deliberately: a citation has to keep rendering after the
    document behind it has been re-processed into different chunks, or
    deleted outright. Storing only ids would leave old turns unreadable.
    """
    return {
        "marker": citation.marker,
        "document_id": str(citation.document_id),
        "document_name": citation.document_name,
        "chunk_id": str(citation.chunk_id),
        "chunk_index": citation.chunk_index,
        "page_number": citation.page_number,
        "excerpt": citation.excerpt,
        "score": citation.score,
    }


class ChatService:
    def __init__(
        self,
        conversations: ConversationRepository,
        messages: MessageRepository,
        collections: CollectionRepository,
        audit: AuditRepository,
        authorizer: NamespaceAuthorizer,
        answers: AnswerService,
        llm: ChatProvider,
        settings: Settings,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._collections = collections
        self._audit = audit
        self._authorizer = authorizer
        self._answers = answers
        self._llm = llm
        self._settings = settings

    # ------------------------------------------------------- conversations ---
    async def create(
        self,
        user: User,
        namespace_id: uuid.UUID,
        *,
        collection_id: uuid.UUID,
        title: str | None = None,
    ) -> Conversation:
        # Viewers may ask questions: reading is what a viewer is for.
        await self._authorizer.authorize(user, namespace_id, minimum_role=NamespaceRole.VIEWER)

        collection = await self._collections.get(namespace_id, collection_id)
        if collection is None:
            raise NotFound("Collection not found.")

        conversation = Conversation(
            namespace_id=namespace_id,
            user_id=user.id,
            title=(title or "").strip() or None,
            model=self._llm.model,
            collection_ids=[str(collection_id)],
        )
        await self._conversations.add(conversation)
        await self._audit.record(
            action=AuditAction.CREATE,
            event="conversation.create",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="conversation",
            resource_id=conversation.id,
        )
        return conversation

    async def get(
        self, user: User, namespace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation:
        await self._authorizer.authorize(user, namespace_id, minimum_role=NamespaceRole.VIEWER)
        conversation = await self._conversations.get(namespace_id, user.id, conversation_id)
        if conversation is None:
            # Covers "does not exist" and "belongs to someone else" with one
            # response, so the API cannot be used to discover that another
            # user has a thread with a given id.
            raise NotFound("Conversation not found.")
        return conversation

    async def list_for_user(
        self,
        user: User,
        namespace_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        include_archived: bool = False,
    ) -> tuple[list[Conversation], int]:
        await self._authorizer.authorize(user, namespace_id, minimum_role=NamespaceRole.VIEWER)
        return await self._conversations.list_for_user(
            namespace_id,
            user.id,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )

    async def list_messages(
        self,
        user: User,
        namespace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[Message], int]:
        conversation = await self.get(user, namespace_id, conversation_id)
        return await self._messages.list_for_conversation(
            conversation.id, limit=limit, offset=offset
        )

    async def rename(
        self, user: User, namespace_id: uuid.UUID, conversation_id: uuid.UUID, *, title: str
    ) -> Conversation:
        conversation = await self.get(user, namespace_id, conversation_id)
        cleaned = title.strip()
        if not cleaned:
            raise ValidationFailed("A title is required.")
        conversation.title = cleaned[: self._settings.chat_title_length]
        await self._conversations.flush()
        return conversation

    async def delete(self, user: User, namespace_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        conversation = await self.get(user, namespace_id, conversation_id)
        # Soft-deleted: an audit entry may reference the thread, and a user
        # restoring a mis-click is cheaper than an irreversible delete.
        conversation.deleted_at = datetime.now(UTC)
        await self._conversations.flush()
        await self._audit.record(
            action=AuditAction.DELETE,
            event="conversation.delete",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="conversation",
            resource_id=conversation.id,
        )

    # ------------------------------------------------------------ history ---
    async def history_for(self, conversation: Conversation) -> list[ChatMessage]:
        """The tail of the thread, within both bounds, oldest first.

        Citations are not replayed. The markers in an old assistant turn
        refer to *that* turn's passages, and re-sending them would invite the
        model to reuse a number that now means something else.
        """
        limit = self._settings.chat_history_messages
        if limit == 0:
            return []

        recent = await self._messages.recent_turns(conversation.id, limit=limit)

        budget = self._settings.chat_history_max_chars
        kept: list[ChatMessage] = []
        # Walked backwards so that when the budget runs out it is the oldest
        # turns that are dropped, not the ones nearest the question.
        for message in reversed(recent):
            if message.role not in (MessageRole.USER, MessageRole.ASSISTANT):
                continue
            content = message.content.strip()
            if not content:
                continue
            if len(content) > budget:
                break
            budget -= len(content)
            kept.append(ChatMessage(role=message.role.value, content=content))
        return list(reversed(kept))

    # ------------------------------------------------------------ messages ---
    async def record_question(self, conversation: Conversation, question: str) -> Message:
        message = Message(
            conversation_id=conversation.id,
            sequence=await self._messages.next_sequence(conversation.id),
            role=MessageRole.USER,
            content=question,
        )
        await self._messages.add(message)

        if conversation.title is None:
            conversation.title = question[: self._settings.chat_title_length]
        conversation.message_count += 1
        conversation.last_message_at = datetime.now(UTC)
        await self._conversations.flush()
        return message

    async def record_answer(
        self, conversation: Conversation, outcome: StreamOutcome, *, latency_ms: int | None = None
    ) -> Message | None:
        """Persist the assistant turn, however the stream ended.

        Returns ``None`` when there is nothing worth storing - an upstream
        failure before a single token arrived leaves the thread with just the
        question, which is the honest record of what happened.
        """
        if outcome.persisted:
            return None
        if not outcome.text and outcome.error is None:
            return None

        message = Message(
            conversation_id=conversation.id,
            sequence=await self._messages.next_sequence(conversation.id),
            role=MessageRole.ASSISTANT,
            content=outcome.text,
            citations=[citation_payload(c) for c in outcome.citations],
            model=outcome.model or self._llm.model,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            latency_ms=latency_ms,
            finish_reason=outcome.finish_reason,
            error=outcome.error,
        )
        await self._messages.add(message)

        conversation.message_count += 1
        conversation.last_message_at = datetime.now(UTC)
        conversation.total_tokens += (outcome.prompt_tokens or 0) + (outcome.completion_tokens or 0)
        await self._conversations.flush()
        outcome.persisted = True
        return message

    # ----------------------------------------------------------- streaming ---
    async def prepare_turn(
        self,
        user: User,
        namespace_id: uuid.UUID,
        conversation: Conversation,
        *,
        question: str,
        collection_id: uuid.UUID | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> AnswerContext:
        """Retrieve evidence for the next turn, with the thread's history."""
        question = question.strip()
        if not question:
            raise ValidationFailed("A question is required.")
        if len(question) > MAX_QUERY_LENGTH:
            raise ValidationFailed(f"A question may be at most {MAX_QUERY_LENGTH} characters.")

        target = collection_id or self._collection_of(conversation)
        history = await self.history_for(conversation)
        return await self._answers.prepare(
            user,
            namespace_id,
            target,
            question=question,
            top_k=top_k,
            min_score=min_score,
            history=history,
        )

    async def stream_turn(
        self, context: AnswerContext, outcome: StreamOutcome
    ) -> AsyncIterator[str]:
        """Yield answer text as it arrives, updating ``outcome`` in place.

        Deltas are yielded raw. Citation markers are only resolved once the
        whole answer is in hand: a marker can be split across two deltas, and
        a half-written ``[1`` must not be interpreted or rewritten.
        """
        started = time.monotonic()
        buffer: list[str] = []
        outcome.retrieved_count = len(context.retrieved)

        stream = self._llm.stream(self._answers.build_request(context))
        try:
            async for chunk in stream:
                if chunk.done:
                    outcome.model = chunk.model or self._llm.model
                    outcome.finish_reason = chunk.finish_reason
                    outcome.prompt_tokens = chunk.prompt_tokens
                    outcome.completion_tokens = chunk.completion_tokens
                    continue
                if chunk.delta:
                    buffer.append(chunk.delta)
                    yield chunk.delta
        except LLMError as exc:
            outcome.text = "".join(buffer)
            outcome.error = str(exc)
            outcome.finish_reason = FINISH_UPSTREAM_ERROR
            self._finalize(context, outcome)
            raise translate_llm_error(exc) from exc
        finally:
            # Runs on the normal path, on an upstream failure, and when the
            # consumer abandons the generator. Closing the upstream stream is
            # what actually stops a disconnected client's tokens being paid
            # for, so it must not be skipped.
            await _aclose(stream)
            outcome.text = outcome.text or "".join(buffer)
            logger.debug(
                "Stream ended after %.1fms with %d character(s)",
                (time.monotonic() - started) * 1000,
                len(outcome.text),
            )

        self._finalize(context, outcome)

    def _finalize(self, context: AnswerContext, outcome: StreamOutcome) -> None:
        """Resolve markers over whatever text was produced.

        Applied to partial answers too: a disconnected or truncated answer
        still shows markers, and they have to mean the same thing there as in
        a complete one.
        """
        if not outcome.text:
            outcome.citations = []
            return
        resolved = self._answers.resolve(outcome.text, context)
        outcome.text = resolved.text
        outcome.citations = resolved.citations
        outcome.refused = resolved.refused

    @staticmethod
    def _collection_of(conversation: Conversation) -> uuid.UUID:
        ids = conversation.collection_ids or []
        if not ids:
            raise ValidationFailed(
                "This conversation is not bound to a collection; supply collection_id."
            )
        try:
            return uuid.UUID(str(ids[0]))
        except ValueError as exc:  # pragma: no cover - defensive
            raise ServiceUnavailable("This conversation references an invalid collection.") from exc


async def _aclose(stream: object) -> None:
    """Close an async generator, ignoring one that is already finished."""
    closer = getattr(stream, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # pragma: no cover - cleanup must not mask the real error
        logger.debug("Closing the upstream stream failed", exc_info=True)
