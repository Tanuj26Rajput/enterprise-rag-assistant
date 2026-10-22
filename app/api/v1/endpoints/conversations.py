"""Conversations, their messages, and the streaming chat endpoint."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import ChatServiceDep, CurrentUser, PageParamsDep, SessionDep
from app.api.sse import SSE_HEADERS, event
from app.core.exceptions import AppError
from app.db.models import Conversation
from app.llm.prompts import REFUSAL_MESSAGE
from app.schemas.chat import (
    ChatRequest,
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
)
from app.schemas.common import Page
from app.services.chat import (
    FINISH_CLIENT_DISCONNECT,
    ChatService,
    StreamOutcome,
    citation_payload,
)
from app.services.generation import AnswerContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/namespaces/{namespace_id}/conversations", tags=["conversations"])


@router.post(
    "",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
)
async def create_conversation(
    namespace_id: uuid.UUID,
    payload: ConversationCreate,
    user: CurrentUser,
    service: ChatServiceDep,
    session: SessionDep,
) -> ConversationRead:
    conversation = await service.create(
        user, namespace_id, collection_id=payload.collection_id, title=payload.title
    )
    await session.commit()
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=Page[ConversationRead], summary="List your conversations")
async def list_conversations(
    namespace_id: uuid.UUID,
    user: CurrentUser,
    service: ChatServiceDep,
    page: PageParamsDep,
    include_archived: bool = False,
) -> Page[ConversationRead]:
    rows, total = await service.list_for_user(
        user,
        namespace_id,
        limit=page.limit,
        offset=page.offset,
        include_archived=include_archived,
    )
    return Page.build([ConversationRead.model_validate(row) for row in rows], total, page)


@router.get("/{conversation_id}", response_model=ConversationRead, summary="Read a conversation")
async def read_conversation(
    namespace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: CurrentUser,
    service: ChatServiceDep,
) -> ConversationRead:
    conversation = await service.get(user, namespace_id, conversation_id)
    return ConversationRead.model_validate(conversation)


@router.patch(
    "/{conversation_id}", response_model=ConversationRead, summary="Rename a conversation"
)
async def rename_conversation(
    namespace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    user: CurrentUser,
    service: ChatServiceDep,
    session: SessionDep,
) -> ConversationRead:
    conversation = await service.rename(user, namespace_id, conversation_id, title=payload.title)
    await session.commit()
    return ConversationRead.model_validate(conversation)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
)
async def delete_conversation(
    namespace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: CurrentUser,
    service: ChatServiceDep,
    session: SessionDep,
) -> None:
    await service.delete(user, namespace_id, conversation_id)
    await session.commit()


@router.get(
    "/{conversation_id}/messages",
    response_model=Page[MessageRead],
    summary="List a conversation's messages",
)
async def list_messages(
    namespace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: CurrentUser,
    service: ChatServiceDep,
    page: PageParamsDep,
) -> Page[MessageRead]:
    rows, total = await service.list_messages(
        user, namespace_id, conversation_id, limit=page.limit, offset=page.offset
    )
    return Page.build([MessageRead.model_validate(row) for row in rows], total, page)


@router.post(
    "/{conversation_id}/messages",
    summary="Ask a question and stream the answer (SSE)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "An event stream. `start` carries the turn's metadata, `token` carries "
                "answer text as it arrives, `done` carries the final answer with its "
                "citations, and `error` reports an upstream failure."
            ),
        }
    },
)
async def stream_message(
    namespace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ChatRequest,
    request: Request,
    user: CurrentUser,
    service: ChatServiceDep,
    session: SessionDep,
) -> StreamingResponse:
    """Stream one turn.

    Everything that can fail with a status code - authentication, ownership,
    an unknown collection, an unusable question - is done *before* the
    response starts. Once a 200 and the first byte are on the wire the status
    line is spent, and a failure can only be reported inside the stream, so
    as little as possible is left to go wrong after that point.
    """
    conversation = await service.get(user, namespace_id, conversation_id)
    context = await service.prepare_turn(
        user,
        namespace_id,
        conversation,
        question=payload.question,
        collection_id=payload.collection_id,
        top_k=payload.top_k,
        min_score=payload.min_score,
    )

    # The question is durable before any token is requested: a failure mid
    # answer must not lose what the user asked.
    await service.record_question(conversation, context.question)
    await session.commit()

    generator = _turn(
        service=service,
        session=session,
        request=request,
        conversation=conversation,
        context=context,
    )
    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


async def _turn(
    *,
    service: ChatService,
    session: SessionDep,
    request: Request,
    conversation: Conversation,
    context: AnswerContext,
) -> AsyncIterator[str]:
    started = time.monotonic()
    outcome = StreamOutcome(conversation_id=conversation.id, question=context.question)

    yield event(
        "start",
        {
            "conversation_id": str(conversation.id),
            "model": conversation.model,
            "retrieved_count": len(context.retrieved),
        },
    )

    if context.is_empty:
        # No evidence: refuse in-band rather than calling the model. The
        # client still gets a well-formed stream, so it needs no second code
        # path for "nothing to say".
        outcome.text = REFUSAL_MESSAGE
        outcome.refused = True
        outcome.finish_reason = "no_evidence"
        yield event("token", {"delta": REFUSAL_MESSAGE})
        await _persist(service, session, conversation, outcome, started)
        yield event("done", _done_payload(outcome))
        return

    failure: tuple[str, str] | None = None
    try:
        async for delta in service.stream_turn(context, outcome):
            if await request.is_disconnected():
                # A client that has gone away must stop costing tokens.
                # Breaking out closes the service's generator, whose `finally`
                # closes the upstream stream.
                outcome.finish_reason = FINISH_CLIENT_DISCONNECT
                logger.info("Client disconnected from conversation %s", conversation.id)
                break
            yield event("token", {"delta": delta})
    except AppError as exc:
        # Translated from the provider's error by the service. The status line
        # is already 200, so in-band is the only way left to report it.
        logger.warning("Stream failed for conversation %s: %s", conversation.id, exc.message)
        failure = (exc.code, exc.message)
    except Exception:
        logger.exception("Unexpected failure streaming conversation %s", conversation.id)
        failure = ("internal_error", "The answer could not be completed.")
    finally:
        # Best effort, and deliberately in `finally`: it also covers the case
        # where the consumer abandons this generator, which is how an abrupt
        # disconnect arrives. If the task is being cancelled outright the
        # write may not get a chance to run - the question is already durable,
        # so what is lost is at worst a partial answer.
        await _persist(service, session, conversation, outcome, started)

    if failure is not None:
        yield event("error", {"code": failure[0], "message": failure[1]})
        return

    if outcome.finish_reason == FINISH_CLIENT_DISCONNECT:
        # Nothing is listening; the partial turn has been saved and there is
        # nobody left to send a final event to.
        return

    yield event("done", _done_payload(outcome))


async def _persist(
    service: ChatService,
    session: SessionDep,
    conversation: Conversation,
    outcome: StreamOutcome,
    started: float,
) -> None:
    """Store the assistant turn. Never allowed to break the stream."""
    try:
        await service.record_answer(
            conversation, outcome, latency_ms=int((time.monotonic() - started) * 1000)
        )
        await session.commit()
    except Exception:
        logger.exception("Failed to persist the assistant turn for %s", conversation.id)
        await session.rollback()


def _done_payload(outcome: StreamOutcome) -> dict[str, object]:
    """The final event.

    It repeats the whole answer rather than only ending the stream: a client
    that dropped a token, or that chose not to render incrementally, gets a
    canonical copy - and the citations, which cannot be sent earlier because
    a marker is only resolvable once the text that contains it is complete.
    """
    return {
        "answer": outcome.text,
        "citations": [citation_payload(c) for c in outcome.citations],
        "has_sufficient_evidence": not outcome.refused,
        "grounded": outcome.grounded,
        "model": outcome.model,
        "retrieved_count": outcome.retrieved_count,
        "finish_reason": outcome.finish_reason,
        "truncated": outcome.finish_reason == "length",
        "usage": {
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
        },
    }
