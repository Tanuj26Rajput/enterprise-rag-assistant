"""Splitting extracted text into overlapping chunks.

Design notes:

* Chunks are built by packing whole *segments* (paragraphs, then sentences,
  then words) up to a token budget, never by slicing a token array and
  decoding it back. A chunk is therefore always a verbatim substring of the
  source, which is what makes ``char_start``/``char_end`` meaningful and lets
  a citation be highlighted in the original document.
* The tokenizer is only ever asked to *count*. That keeps the splitter correct
  even when tiktoken's encoding files are unavailable (air-gapped build,
  first run with no network), where it falls back to an estimator.
* Pages are concatenated with a recorded offset for each, so every chunk can
  be attributed to the page its first character came from.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from app.ingestion.extraction import ExtractedDocument

logger = logging.getLogger(__name__)

PAGE_SEPARATOR = "\n\n"

# Paragraph first, then sentence, then whitespace: the earlier a boundary
# appears here, the more semantic it is, so it is preferred while a segment
# is still too large to fit a chunk on its own.
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


class TokenCounter:
    """Counts tokens with tiktoken, falling back to a character estimate."""

    #: Average characters per token for English text in the cl100k family.
    _FALLBACK_CHARS_PER_TOKEN = 4

    def __init__(self, model: str) -> None:
        self._model = model
        self._encoding = _load_encoding(model)

    def count(self, text: str) -> int:
        if self._encoding is not None:
            return len(self._encoding.encode(text, disallowed_special=()))
        # Round up so that an estimate never reports a non-empty string as
        # costing zero tokens, which would let the packer loop forever.
        return max(1, -(-len(text) // self._FALLBACK_CHARS_PER_TOKEN)) if text else 0

    @property
    def is_exact(self) -> bool:
        return self._encoding is not None


@lru_cache(maxsize=8)
def _load_encoding(model: str):
    try:
        import tiktoken

        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        logger.warning(
            "tiktoken encoding unavailable for model %r; using character-based "
            "token estimation. Chunk sizes will be approximate.",
            model,
        )
        return None


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage, positioned within the source text."""

    index: int
    content: str
    token_count: int
    char_start: int
    char_end: int
    page_number: int | None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Segment:
    text: str
    start: int
    end: int
    tokens: int


def _flatten(document: ExtractedDocument) -> tuple[str, list[tuple[int, int | None]]]:
    """Join pages into one string plus a list of ``(start_offset, page)``."""
    parts: list[str] = []
    offsets: list[tuple[int, int | None]] = []
    cursor = 0
    for page in document.pages:
        offsets.append((cursor, page.page_number))
        parts.append(page.text)
        cursor += len(page.text) + len(PAGE_SEPARATOR)
    return PAGE_SEPARATOR.join(parts), offsets


def _page_for_offset(offsets: list[tuple[int, int | None]], offset: int) -> int | None:
    page: int | None = None
    for start, number in offsets:
        if start > offset:
            break
        page = number
    return page


def _split_segments(text: str, counter: TokenCounter, max_tokens: int) -> list[_Segment]:
    """Break text into segments that each fit inside one chunk."""

    def build(fragment: str, start: int) -> _Segment:
        return _Segment(fragment, start, start + len(fragment), counter.count(fragment))

    def subdivide(segment: _Segment, pattern: re.Pattern[str]) -> list[_Segment]:
        pieces: list[_Segment] = []
        cursor = 0
        for piece in pattern.split(segment.text):
            if not piece:
                continue
            found = segment.text.find(piece, cursor)
            if found == -1:  # pragma: no cover - defensive
                found = cursor
            pieces.append(build(piece, segment.start + found))
            cursor = found + len(piece)
        return pieces or [segment]

    # Start from paragraphs, then subdivide anything still too large.
    segments: list[_Segment] = []
    cursor = 0
    for paragraph in _PARAGRAPH_RE.split(text):
        if not paragraph.strip():
            cursor += len(paragraph)
            continue
        found = text.find(paragraph, cursor)
        if found == -1:  # pragma: no cover - defensive
            found = cursor
        segments.append(build(paragraph, found))
        cursor = found + len(paragraph)

    for pattern in (_SENTENCE_RE, _WHITESPACE_RE):
        if all(segment.tokens <= max_tokens for segment in segments):
            break
        expanded: list[_Segment] = []
        for segment in segments:
            if segment.tokens <= max_tokens:
                expanded.append(segment)
            else:
                expanded.extend(subdivide(segment, pattern))
        segments = expanded

    return [segment for segment in segments if segment.text.strip()]


def chunk_document(
    document: ExtractedDocument,
    *,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    embedding_model: str,
) -> list[Chunk]:
    """Split an extracted document into overlapping chunks."""
    if chunk_overlap_tokens >= chunk_size_tokens:
        raise ValueError("chunk_overlap_tokens must be smaller than chunk_size_tokens")

    counter = TokenCounter(embedding_model)
    text, page_offsets = _flatten(document)
    segments = _split_segments(text, counter, chunk_size_tokens)
    if not segments:
        return []

    chunks: list[Chunk] = []
    window: list[_Segment] = []
    window_tokens = 0

    def emit() -> None:
        if not window:
            return
        start, end = window[0].start, window[-1].end
        content = text[start:end].strip()
        if not content:
            return
        # .strip() may have moved the start; realign so the offsets still
        # point at the exact characters stored in `content`.
        offset = text[start:end].index(content) if content in text[start:end] else 0
        chunks.append(
            Chunk(
                index=len(chunks),
                content=content,
                token_count=counter.count(content),
                char_start=start + offset,
                char_end=start + offset + len(content),
                page_number=_page_for_offset(page_offsets, start + offset),
            )
        )

    for segment in segments:
        if window and window_tokens + segment.tokens > chunk_size_tokens:
            emit()
            # Carry the tail of the window into the next chunk so a passage
            # spanning a boundary is still retrievable in one piece.
            carried: list[_Segment] = []
            carried_tokens = 0
            for previous in reversed(window):
                if carried_tokens + previous.tokens > chunk_overlap_tokens:
                    break
                carried.insert(0, previous)
                carried_tokens += previous.tokens
            window, window_tokens = carried, carried_tokens

        window.append(segment)
        window_tokens += segment.tokens

    emit()
    return chunks
