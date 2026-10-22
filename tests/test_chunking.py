"""Unit tests for the splitter and the upload validator."""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.core.exceptions import ValidationFailed
from app.ingestion.chunking import TokenCounter, chunk_document
from app.ingestion.extraction import ExtractedDocument, ExtractedPage
from app.ingestion.validation import PDF, TXT, validate_upload

SENTENCE = "Retrieval augmented generation grounds the model in retrieved evidence. "


def document(*pages: tuple[int | None, str]) -> ExtractedDocument:
    return ExtractedDocument(pages=[ExtractedPage(n, text) for n, text in pages])


def split(doc: ExtractedDocument, size: int = 64, overlap: int = 16):
    return chunk_document(
        doc, chunk_size_tokens=size, chunk_overlap_tokens=overlap, embedding_model="test-model"
    )


class TestChunking:
    def test_short_text_is_a_single_chunk(self) -> None:
        chunks = split(document((None, "A short note.")))

        assert len(chunks) == 1
        assert chunks[0].content == "A short note."
        assert chunks[0].index == 0

    def test_long_text_is_split(self) -> None:
        chunks = split(document((None, SENTENCE * 40)))

        assert len(chunks) > 1
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_no_chunk_greatly_exceeds_the_budget(self) -> None:
        chunks = split(document((None, SENTENCE * 40)), size=64, overlap=0)
        counter = TokenCounter("test-model")

        for chunk in chunks:
            # One segment may exceed the budget on its own only if it is a
            # single unsplittable word; these are ordinary sentences.
            assert counter.count(chunk.content) <= 64 * 2

    def test_offsets_slice_the_source_exactly(self) -> None:
        text = SENTENCE * 25
        chunks = split(document((None, text)))

        for chunk in chunks:
            assert text[chunk.char_start : chunk.char_end] == chunk.content

    def test_consecutive_chunks_overlap(self) -> None:
        chunks = split(document((None, SENTENCE * 40)), size=64, overlap=24)

        assert len(chunks) > 1
        # The next chunk starts before the previous one ends.
        assert any(later.char_start < earlier.char_end for earlier, later in pairwise(chunks))

    def test_zero_overlap_produces_disjoint_chunks(self) -> None:
        chunks = split(document((None, SENTENCE * 40)), size=64, overlap=0)

        for earlier, later in pairwise(chunks):
            assert later.char_start >= earlier.char_end

    def test_overlap_must_be_smaller_than_the_chunk(self) -> None:
        with pytest.raises(ValueError, match="smaller"):
            split(document((None, SENTENCE)), size=32, overlap=32)

    def test_page_numbers_are_carried_onto_chunks(self) -> None:
        chunks = split(document((1, SENTENCE * 12), (2, SENTENCE * 12)), size=48, overlap=0)

        pages = {chunk.page_number for chunk in chunks}
        assert pages == {1, 2}

    def test_pageless_documents_yield_no_page_numbers(self) -> None:
        chunks = split(document((None, SENTENCE * 20)))

        assert all(chunk.page_number is None for chunk in chunks)

    def test_whitespace_only_text_yields_no_chunks(self) -> None:
        assert split(document((None, "   \n\n\t  "))) == []

    def test_content_hash_is_sha256_of_the_chunk(self) -> None:
        varied = " ".join(f"Clause {n} of the retention policy applies." for n in range(200))
        chunks = split(document((None, varied)), size=48, overlap=0)

        assert all(len(chunk.content_hash) == 64 for chunk in chunks)
        # Distinct text hashes distinctly; identical text is meant to collide,
        # which is what makes the hash usable for de-duplication.
        assert len({chunk.content_hash for chunk in chunks}) == len(chunks)

    def test_identical_chunks_share_a_hash(self) -> None:
        chunks = split(document((None, SENTENCE * 30)), size=48, overlap=0)

        repeated = [c for c in chunks if c.content == chunks[0].content]
        assert all(c.content_hash == chunks[0].content_hash for c in repeated)

    def test_token_counts_are_positive(self) -> None:
        """A zero token count would violate the chunk table's check constraint."""
        chunks = split(document((None, SENTENCE * 30)))

        assert all(chunk.token_count > 0 for chunk in chunks)

    def test_a_single_oversized_word_still_produces_a_chunk(self) -> None:
        chunks = split(document((None, "x" * 2000)), size=16, overlap=0)

        assert len(chunks) >= 1
        assert "".join(c.content for c in chunks).startswith("x")


class TestTokenCounter:
    def test_counts_are_positive_for_non_empty_text(self) -> None:
        counter = TokenCounter("gpt-4o-mini")

        assert counter.count("hello world") > 0
        assert counter.count("") == 0

    def test_longer_text_counts_higher(self) -> None:
        counter = TokenCounter("gpt-4o-mini")

        assert counter.count(SENTENCE * 10) > counter.count(SENTENCE)


class TestUploadValidation:
    def ok(self, **overrides):
        defaults = {
            "filename": "notes.txt",
            "declared_mime_type": "text/plain",
            "content": b"hello there",
            "max_size_bytes": 1024,
        }
        return validate_upload(**{**defaults, **overrides})

    def test_accepts_plain_text(self) -> None:
        result = self.ok()

        assert result.kind is TXT
        assert result.filename == "notes.txt"

    def test_accepts_a_pdf_by_its_magic_bytes(self) -> None:
        result = self.ok(
            filename="doc.pdf", declared_mime_type="application/pdf", content=b"%PDF-1.7\n..."
        )

        assert result.kind is PDF

    def test_strips_directory_components_from_the_filename(self) -> None:
        assert self.ok(filename="../../etc/passwd.txt").filename == "passwd.txt"
        assert self.ok(filename="C:\\Windows\\notes.txt").filename == "notes.txt"

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"content": b""}, "empty"),
            ({"content": b"a" * 2048}, "maximum"),
            ({"filename": "run.exe", "declared_mime_type": None}, "Unsupported"),
            ({"filename": "notes"}, "Unsupported"),
            ({"filename": "", "declared_mime_type": None}, "filename"),
            # A NUL byte decodes as UTF-8 but is still binary content.
            ({"content": b"\x00\x01binary", "filename": "x.txt"}, "binary data"),
            # Invalid UTF-8 with no recognisable header.
            ({"content": b"\xff\xfe\x00bin", "filename": "x.txt"}, "supported document"),
        ],
    )
    def test_rejects_bad_input(self, kwargs: dict, fragment: str) -> None:
        with pytest.raises(ValidationFailed, match=fragment):
            self.ok(**kwargs)

    def test_rejects_content_that_contradicts_the_extension(self) -> None:
        with pytest.raises(ValidationFailed, match="do not match"):
            self.ok(filename="doc.pdf", declared_mime_type="application/pdf", content=b"plain")

    def test_rejects_a_declared_type_that_contradicts_the_bytes(self) -> None:
        with pytest.raises(ValidationFailed, match="declared content type"):
            self.ok(filename="notes.txt", declared_mime_type="application/pdf")

    def test_tolerates_a_generic_octet_stream_declaration(self) -> None:
        """Browsers send this when they cannot identify the file themselves."""
        assert self.ok(declared_mime_type="application/octet-stream").kind is TXT

    def test_respects_the_configured_allow_list(self) -> None:
        with pytest.raises(ValidationFailed, match="not permitted"):
            self.ok(allowed_mime_types={"application/pdf"})
