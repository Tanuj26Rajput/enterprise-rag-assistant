"""Persistence for documents and their chunks."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, DocumentStatus


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, namespace_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        stmt = select(Document).where(
            Document.id == document_id,
            Document.namespace_id == namespace_id,
            Document.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def get_any(self, document_id: uuid.UUID) -> Document | None:
        """Fetch by id alone, including soft-deleted rows.

        For background jobs only. A job carries a document id and no user, so
        there is no namespace to scope by - and a delete job's whole purpose
        is to act on a row that :meth:`get` deliberately hides. Every
        user-facing path must use :meth:`get` instead; this one has no tenant
        predicate and so no tenant isolation of its own.
        """
        return await self._session.scalar(select(Document).where(Document.id == document_id))

    async def find_duplicate(self, collection_id: uuid.UUID, checksum: str) -> Document | None:
        """Find a live document with identical content in this collection.

        Mirrors the partial unique index, so the service can return a clean
        409 instead of surfacing an IntegrityError.
        """
        stmt = select(Document).where(
            Document.collection_id == collection_id,
            Document.checksum == checksum,
            Document.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def list_for_collection(
        self,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: DocumentStatus | None = None,
    ) -> tuple[list[Document], int]:
        predicate = [
            Document.namespace_id == namespace_id,
            Document.collection_id == collection_id,
            Document.deleted_at.is_(None),
        ]
        if status is not None:
            predicate.append(Document.status == status)

        total = await self._session.scalar(
            select(func.count()).select_from(Document).where(*predicate)
        )
        rows = await self._session.scalars(
            select(Document)
            .where(*predicate)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def add(self, document: Document) -> Document:
        self._session.add(document)
        await self._session.flush()
        return document

    async def flush(self) -> None:
        await self._session.flush()


class DocumentChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_document(
        self, document_id: uuid.UUID, chunks: list[DocumentChunk]
    ) -> None:
        """Swap a document's chunks atomically.

        Re-processing deletes the old chunks rather than updating them: chunk
        boundaries shift when the splitter or its configuration changes, so a
        stale chunk id would resolve a citation to the wrong passage.
        """
        await self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        self._session.add_all(chunks)
        await self._session.flush()

    async def get_by_vector_ids(
        self, namespace_id: uuid.UUID, vector_ids: list[str]
    ) -> dict[str, tuple[DocumentChunk, Document]]:
        """Resolve vector ids to live chunks, scoped to one namespace.

        Retrieval hits are resolved through this rather than trusted as they
        come back from the vector store. Two things fall out of that:

        * The join to ``documents`` applies the soft-delete predicate, so a
          deleted document cannot be cited even if its vectors are still in
          the index - which makes vector cleanup a hygiene task rather than
          the thing standing between a deleted document and a user.
        * The namespace predicate is enforced a second time, in the system of
          record. The store already filtered by tenant; this means a bug in
          that filter, or a collection name collision, still cannot leak
          another tenant's text.
        """
        if not vector_ids:
            return {}

        stmt = (
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.vector_id.in_(vector_ids),
                Document.namespace_id == namespace_id,
                Document.deleted_at.is_(None),
            )
        )
        rows = await self._session.execute(stmt)
        return {chunk.vector_id: (chunk, document) for chunk, document in rows if chunk.vector_id}

    async def list_for_document(
        self, document_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[DocumentChunk], int]:
        total = await self._session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        rows = await self._session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)
