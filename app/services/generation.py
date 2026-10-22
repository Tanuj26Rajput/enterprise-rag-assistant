"""Answer generation grounded in retrieved passages.

The model is never trusted to report *what* it cited. It emits bracketed
markers; this module resolves those markers against the passages it was
actually given and builds every citation from the database rows behind them.
A marker the model invents resolves to nothing and is removed from the text,
so the worst case for a hallucinated citation is a missing one - never a
plausible reference to a document that does not exist, or to a page that was
never retrieved.

Three guarantees follow from that, and each is tested:

* **Only authorized passages reach the prompt.** The context is built from
  :class:`~app.services.retrieval.RetrievalService` results, which are
  namespace-filtered inside the vector store *and* re-resolved through the
  database. Nothing else is ever put in front of the model.
* **Citations cannot be invented**, because they are not parsed out of the
  model's prose at all. Document names, pages and excerpts come from the
  retrieved chunks; the model only chooses *which* of them to point at.
* **Absence of evidence is reportable.** If retrieval returns nothing the
  model is not called at all, and if the passages do not support an answer
  the model is asked to say so in a fixed form that needs no natural-language
  parsing to detect.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailable, UpstreamMisconfigured, ValidationFailed
from app.core.metrics import record_llm_call
from app.db.models import User
from app.llm.base import (
    ChatCompletion,
    ChatMessage,
    ChatProvider,
    ChatRequest,
    LLMConfigurationError,
    LLMError,
)
from app.llm.prompts import (
    INSUFFICIENT_EVIDENCE,
    REFUSAL_MESSAGE,
    SYSTEM_PROMPT,
    PromptSource,
    build_user_prompt,
)
from app.services.retrieval import MAX_QUERY_LENGTH, RetrievalService, RetrievedChunk

logger = logging.getLogger(__name__)

#: Matches one bracketed citation group: [2], [1,3], [ 4 , 5 ].
_MARKER_GROUP = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")

#: Collapses the whitespace left behind when a marker is removed.
_LOOSE_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?])")


@dataclass(frozen=True, slots=True)
class Citation:
    """One passage the answer actually leaned on.

    Every field is copied from the retrieved chunk and its document row. None
    of it is read out of the model's response.
    """

    marker: int
    document_id: uuid.UUID
    document_name: str
    chunk_id: uuid.UUID
    chunk_index: int
    excerpt: str
    score: float
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class AnswerContext:
    """Everything assembled before the model is called.

    Shared by the blocking and streaming paths so that both ground their
    answers in exactly the same way: one retrieval, one prompt, one marker
    resolution. A second implementation for streaming would be a second place
    for a citation guarantee to quietly drift.
    """

    question: str
    retrieved: list[RetrievedChunk]
    sources: list[PromptSource]
    messages: list[ChatMessage]

    @property
    def is_empty(self) -> bool:
        return not self.retrieved


@dataclass(frozen=True, slots=True)
class ResolvedAnswer:
    """Model text after refusal detection and citation resolution."""

    text: str
    citations: list[Citation]
    refused: bool

    @property
    def grounded(self) -> bool:
        return bool(self.citations)


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    answer: str
    citations: list[Citation]
    #: False when nothing was retrieved, or the model reported that the
    #: passages do not support an answer.
    has_sufficient_evidence: bool
    #: True when at least one citation survived resolution. An answer without
    #: one is reported rather than dressed up: the contract asks the model to
    #: cite every claim, so an uncited answer did not keep it.
    grounded: bool
    model: str
    retrieved_count: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    truncated: bool = False


def translate_llm_error(exc: LLMError) -> ServiceUnavailable:
    """Map a provider failure onto the API's error envelope.

    Shared by the blocking and streaming paths. The provider's own wording is
    logged but never returned: upstream errors routinely quote the request
    back, which here would mean echoing the prompt - and with it the
    retrieved passages - to the client.
    """
    if isinstance(exc, LLMConfigurationError):
        # The caller can do nothing about this; an operator must.
        logger.error("Answer generation is misconfigured: %s", exc)
        return UpstreamMisconfigured()
    logger.warning("Answer generation failed: %s", exc)
    return ServiceUnavailable("The answer service is temporarily unavailable. Please try again.")


class AnswerService:
    def __init__(
        self,
        retrieval: RetrievalService,
        llm: ChatProvider,
        settings: Settings,
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        self._settings = settings

    async def prepare(
        self,
        user: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        question: str,
        top_k: int | None = None,
        min_score: float | None = None,
        history: list[ChatMessage] | None = None,
    ) -> AnswerContext:
        """Retrieve evidence and build the prompt, without calling the model.

        Split out so the streaming path can do the same work and then stream,
        and so a caller can discover that there is no evidence *before*
        opening a stream it would immediately have to close.
        """
        question = question.strip()
        if not question:
            raise ValidationFailed("A question is required.")
        if len(question) > MAX_QUERY_LENGTH:
            raise ValidationFailed(f"A question may be at most {MAX_QUERY_LENGTH} characters.")

        # Authorization, tenant filtering and the soft-delete predicate all
        # live in here. This call is the only source of context.
        retrieved = await self._retrieval.search(
            user,
            namespace_id,
            collection_id,
            query=question,
            top_k=top_k,
            min_score=min_score,
        )
        sources = self._build_sources(retrieved)

        messages = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
        # Prior turns sit between the system contract and the current
        # question, so the passages supplied now are the ones nearest the
        # instruction to use them.
        messages.extend(history or [])
        messages.append(ChatMessage(role="user", content=build_user_prompt(question, sources)))

        return AnswerContext(
            question=question, retrieved=retrieved, sources=sources, messages=messages
        )

    def resolve(self, raw: str, context: AnswerContext) -> ResolvedAnswer:
        """Turn raw model text into an answer and its citations.

        The only place markers are interpreted, for both paths.
        """
        refused, text = self._split_refusal(raw.strip())
        text, markers = self._resolve_markers(text, len(context.retrieved))
        citations = (
            [] if refused else self._citations_for(markers, context.retrieved, context.sources)
        )
        if refused:
            text = REFUSAL_MESSAGE if not text else f"{REFUSAL_MESSAGE}\n\n{text}"
        return ResolvedAnswer(text=text, citations=citations, refused=refused)

    def empty_answer(self, retrieved_count: int = 0) -> GeneratedAnswer:
        """The response when nothing could be retrieved."""
        return GeneratedAnswer(
            answer=REFUSAL_MESSAGE,
            citations=[],
            has_sufficient_evidence=False,
            grounded=False,
            model=self._llm.model,
            retrieved_count=retrieved_count,
        )

    async def answer(
        self,
        user: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        question: str,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> GeneratedAnswer:
        context = await self.prepare(
            user,
            namespace_id,
            collection_id,
            question=question,
            top_k=top_k,
            min_score=min_score,
        )
        retrieved = context.retrieved

        if not retrieved:
            # No passages means there is nothing to be grounded in, so there
            # is nothing to ask. Calling the model here would spend money to
            # obtain an ungrounded answer, which is exactly what this
            # endpoint exists to avoid.
            logger.info("No passages retrieved for a question in namespace %s", namespace_id)
            return self.empty_answer()

        completion = await self.complete(context)

        if not completion.content.strip():
            raise ServiceUnavailable("The model returned an empty answer. Please try again.")

        resolved = self.resolve(completion.content, context)
        if not resolved.refused and not resolved.citations:
            logger.warning(
                "Model produced an answer with no resolvable citation for a question in "
                "namespace %s; reporting it as ungrounded",
                namespace_id,
            )

        return GeneratedAnswer(
            answer=resolved.text,
            citations=resolved.citations,
            has_sufficient_evidence=not resolved.refused,
            grounded=resolved.grounded,
            model=completion.model,
            retrieved_count=len(retrieved),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            truncated=completion.was_truncated,
        )

    # -------------------------------------------------------------- prompt ---
    def _build_sources(self, retrieved: list[RetrievedChunk]) -> list[PromptSource]:
        """Number the passages, 1-based, in retrieval order.

        The marker *is* the index into this list, which is what makes
        resolution a lookup rather than an interpretation.
        """
        limit = self._settings.answer_excerpt_chars
        sources = []
        for position, result in enumerate(retrieved, start=1):
            name = result.document.title or result.document.filename
            page = result.chunk.page_number
            label = f"{name}, page {page}" if page is not None else name
            sources.append(
                PromptSource(
                    marker=position,
                    label=label,
                    text=_truncate(result.chunk.content, limit),
                )
            )
        return sources

    def build_request(self, context: AnswerContext) -> ChatRequest:
        return ChatRequest(
            messages=context.messages,
            temperature=self._settings.llm_temperature,
            max_tokens=self._settings.llm_max_tokens,
        )

    async def complete(self, context: AnswerContext) -> ChatCompletion:
        started = time.perf_counter()
        try:
            completion = await self._llm.complete(self.build_request(context))
        except LLMError as exc:
            # Labelled by the provider's own classification rather than by
            # the HTTP status it arrived with: "we were rate limited" and
            # "the provider timed out" need different responses from an
            # operator, and a 429/504 split does not reliably tell them apart.
            record_llm_call(self._llm.model, type(exc).__name__, time.perf_counter() - started)
            raise translate_llm_error(exc) from exc

        record_llm_call(
            self._llm.model,
            "succeeded",
            time.perf_counter() - started,
            tokens=completion.total_tokens or 0,
        )
        return completion

    # ---------------------------------------------------------- citations ---
    @staticmethod
    def _split_refusal(text: str) -> tuple[bool, str]:
        """Detect the refusal sentinel and strip it from the text."""
        if not text.startswith(INSUFFICIENT_EVIDENCE):
            return False, text
        remainder = text[len(INSUFFICIENT_EVIDENCE) :]
        return True, remainder.lstrip(" :.-—\n\t").strip()

    @staticmethod
    def _resolve_markers(text: str, source_count: int) -> tuple[str, list[int]]:
        """Keep the markers that name a real passage; delete the rest.

        A model that cites ``[9]`` when it was given five passages has
        invented a source. Leaving the marker in place would show the reader
        a citation the response cannot substantiate, so it is removed from
        the prose as well as ignored.
        """
        cited: list[int] = []

        def replace(match: re.Match[str]) -> str:
            numbers = [int(part) for part in match.group(1).split(",")]
            valid = [n for n in numbers if 1 <= n <= source_count]
            for number in valid:
                if number not in cited:
                    cited.append(number)
            if invalid := [n for n in numbers if n not in valid]:
                logger.warning(
                    "Dropped fabricated citation marker(s) %s; %d passage(s) were supplied",
                    invalid,
                    source_count,
                )
            return "".join(f"[{n}]" for n in valid)

        cleaned = _MARKER_GROUP.sub(replace, text)
        cleaned = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
        cleaned = _LOOSE_SPACE.sub(" ", cleaned)
        return cleaned.strip(), cited

    def _citations_for(
        self,
        markers: list[int],
        retrieved: list[RetrievedChunk],
        sources: list[PromptSource],
    ) -> list[Citation]:
        """Build a citation per surviving marker, from the database rows.

        Only cited passages are returned. The others were context the model
        read and chose not to rely on; presenting them as support for the
        answer would overstate what it is based on.
        """
        citations = []
        for marker in sorted(markers):
            result = retrieved[marker - 1]
            document = result.document
            citations.append(
                Citation(
                    marker=marker,
                    document_id=document.id,
                    # Same fallback as search: a citation with no label is
                    # not actionable for the reader.
                    document_name=document.title or document.filename,
                    chunk_id=result.chunk.id,
                    chunk_index=result.chunk.chunk_index,
                    # Omitted rather than fabricated for formats that have no
                    # pages - a page the reader cannot find is worse than no
                    # page at all.
                    page_number=result.chunk.page_number,
                    # Exactly the text the model was shown, so a reader can
                    # check the claim against what was actually in evidence.
                    excerpt=sources[marker - 1].text,
                    score=result.score,
                )
            )
        return citations


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
