"""Document upload and the ingestion pipeline."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.db.models import (
    AuditAction,
    AuditOutcome,
    Collection,
    Document,
    DocumentChunk,
    DocumentStatus,
    NamespaceRole,
    User,
)
from app.ingestion.chunking import chunk_document
from app.ingestion.extraction import extract_text
from app.ingestion.validation import SUPPORTED_KINDS, validate_upload
from app.repositories.audit import AuditRepository
from app.repositories.collection import CollectionRepository
from app.repositories.document import DocumentChunkRepository, DocumentRepository
from app.services.authorization import NamespaceAuthorizer, require_active_collection
from app.services.indexing import IndexingError, IndexingService
from app.services.storage import BlobStorage

logger = logging.getLogger(__name__)

_KIND_BY_MIME = {kind.mime_type: kind for kind in SUPPORTED_KINDS}


class DocumentService:
    def __init__(
        self,
        documents: DocumentRepository,
        chunks: DocumentChunkRepository,
        collections: CollectionRepository,
        audit: AuditRepository,
        authorizer: NamespaceAuthorizer,
        storage: BlobStorage,
        settings: Settings,
        indexing: IndexingService | None = None,
    ) -> None:
        self._documents = documents
        self._chunks = chunks
        self._collections = collections
        self._audit = audit
        self._authorizer = authorizer
        self._storage = storage
        self._settings = settings
        # Optional so the ingestion pipeline stays usable - and testable -
        # with no vector backend configured. When it is absent a document
        # still reaches `ready`, but with no vectors, so it will not be
        # retrievable; that is logged rather than silently accepted.
        self._indexing = indexing

    # --------------------------------------------------------------- upload ---
    async def upload(
        self,
        user: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        filename: str | None,
        content_type: str | None,
        content: bytes,
        title: str | None = None,
    ) -> Document:
        """Validate, store and register an upload.

        The document is left in ``pending``; :meth:`process` does the text
        extraction. Splitting the two keeps the request fast and means a
        parser failure is recorded against a row that already exists, rather
        than losing the upload entirely.
        """
        context = await self._authorizer.authorize(user, namespace_id)
        context.require_write()

        collection = await self._collections.get(namespace_id, collection_id)
        if collection is None:
            raise NotFound("Collection not found.")
        require_active_collection(collection)

        try:
            upload = validate_upload(
                filename=filename,
                declared_mime_type=content_type,
                content=content,
                max_size_bytes=self._settings.max_upload_size_bytes,
                allowed_mime_types=self._settings.allowed_upload_types,
            )
        except ValidationFailed as exc:
            await self._audit.record(
                action=AuditAction.CREATE,
                event="document.upload",
                outcome=AuditOutcome.FAILURE,
                actor_id=user.id,
                actor_email=user.email,
                namespace_id=namespace_id,
                resource_type="document",
                resource_label=filename,
                error_message=exc.message,
            )
            raise

        checksum = hashlib.sha256(upload.content).hexdigest()
        duplicate = await self._documents.find_duplicate(collection_id, checksum)
        if duplicate is not None:
            raise Conflict(
                "An identical document already exists in this collection.",
                details={"document_id": str(duplicate.id), "filename": duplicate.filename},
            )

        document_id = uuid.uuid4()
        storage_uri = await self._storage.put(
            namespace_id=namespace_id,
            document_id=document_id,
            extension=upload.extension,
            content=upload.content,
        )

        document = Document(
            id=document_id,
            namespace_id=namespace_id,
            collection_id=collection_id,
            filename=upload.filename,
            title=(title or "").strip() or None,
            mime_type=upload.kind.mime_type,
            size_bytes=upload.size_bytes,
            checksum=checksum,
            storage_uri=storage_uri,
            status=DocumentStatus.PENDING,
            uploaded_by_id=user.id,
            meta={"detected_kind": upload.kind.name},
        )
        await self._documents.add(document)

        collection.document_count += 1
        await self._collections.flush()

        await self._audit.record(
            action=AuditAction.CREATE,
            event="document.upload",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="document",
            resource_id=document.id,
            resource_label=document.filename,
            metadata={"size_bytes": document.size_bytes, "mime_type": document.mime_type},
        )
        return document

    # ------------------------------------------------------------- pipeline ---
    async def process(self, document: Document, *, reraise: bool = False) -> Document:
        """Extract text, chunk it and record the outcome.

        Every failure path ends with the document in ``failed`` carrying an
        explanation, because a document stuck in ``processing`` forever is
        indistinguishable from one the worker never picked up.

        ``reraise`` is for the job runner. The document state written here is
        the same either way; what the runner additionally needs is the
        *exception*, because only it can say whether the failure is worth
        another attempt. Swallowing it would force the runner to guess from a
        status that cannot distinguish "corrupt file" from "provider timed
        out".
        """
        # Captured before the document is updated: re-processing has to
        # subtract the chunks it is replacing from the collection total.
        previous_chunk_count = document.chunk_count or 0

        document.status = DocumentStatus.PROCESSING
        document.processing_started_at = datetime.now(UTC)
        document.error_message = None
        await self._documents.flush()

        try:
            content = await self._storage.get(document.storage_uri)
            kind = _KIND_BY_MIME[document.mime_type]
            extracted = extract_text(content, kind)

            collection = await self._collections.get(document.namespace_id, document.collection_id)
            if collection is None:
                raise ValidationFailed("The collection this document belongs to no longer exists.")

            chunks = chunk_document(
                extracted,
                chunk_size_tokens=self._settings.chunk_size_tokens,
                chunk_overlap_tokens=self._settings.chunk_overlap_tokens,
                embedding_model=collection.embedding_model,
            )
            if not chunks:
                raise ValidationFailed("The document produced no usable text chunks.")

            chunk_rows = [
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    token_count=chunk.token_count,
                    page_number=chunk.page_number,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                )
                for chunk in chunks
            ]
            # Flushed here, so each row has the id that goes into the vector's
            # metadata on the next line.
            await self._chunks.replace_for_document(document.id, chunk_rows)
            await self._index(document, collection, chunk_rows)

            document.status = DocumentStatus.READY
            document.page_count = extracted.page_count
            document.chunk_count = len(chunks)
            document.token_count = sum(chunk.token_count for chunk in chunks)
            document.processed_at = datetime.now(UTC)
            if not document.title and extracted.title:
                document.title = extracted.title[:512]

            await self._apply_collection_delta(document, previous_chunk_count, len(chunks))
            await self._documents.flush()

            await self._audit.record(
                action=AuditAction.UPDATE,
                event="document.process",
                actor_id=document.uploaded_by_id,
                namespace_id=document.namespace_id,
                resource_type="document",
                resource_id=document.id,
                resource_label=document.filename,
                metadata={"chunks": len(chunks), "pages": extracted.page_count},
            )
            logger.info("Processed document %s into %d chunks", document.id, len(chunks))

        except Exception as exc:
            # ValidationFailed and IndexingError both carry messages written
            # for the operator - a misconfigured embedding model is something
            # they must be told, not something to hide behind "failed".
            if isinstance(exc, ValidationFailed):
                message = exc.message
            elif isinstance(exc, IndexingError):
                message = str(exc)
            else:
                message = "Processing failed."
            if not isinstance(exc, ValidationFailed | IndexingError):
                # Unexpected failures are logged in full but reported to the
                # client generically: a parser traceback can disclose paths.
                logger.exception("Processing failed for document %s", document.id)

            document.status = DocumentStatus.FAILED
            document.error_message = message
            document.processed_at = datetime.now(UTC)
            await self._documents.flush()

            await self._audit.record(
                action=AuditAction.UPDATE,
                event="document.process",
                outcome=AuditOutcome.FAILURE,
                actor_id=document.uploaded_by_id,
                namespace_id=document.namespace_id,
                resource_type="document",
                resource_id=document.id,
                resource_label=document.filename,
                error_message=message,
            )
            if reraise:
                raise

        return document

    async def _index(
        self, document: Document, collection: Collection, chunk_rows: list[DocumentChunk]
    ) -> None:
        """Embed the chunks, unless no vector backend is configured."""
        if self._indexing is None:
            logger.warning(
                "No vector backend configured; document %s is processed but not retrievable",
                document.id,
            )
            return
        await self._indexing.index_document(document, collection, chunk_rows)

    async def _apply_collection_delta(
        self, document: Document, previous: int, current: int
    ) -> None:
        """Keep the collection's denormalised chunk total in step."""
        collection: Collection | None = await self._collections.get(
            document.namespace_id, document.collection_id
        )
        if collection is not None:
            collection.chunk_count = max(0, collection.chunk_count - previous + current)

    # ----------------------------------------------------------------- read ---
    async def list_for_collection(
        self,
        user: User,
        namespace_id: uuid.UUID,
        collection_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        status: DocumentStatus | None = None,
    ) -> tuple[list[Document], int]:
        await self._authorizer.authorize(user, namespace_id)
        if await self._collections.get(namespace_id, collection_id) is None:
            raise NotFound("Collection not found.")
        return await self._documents.list_for_collection(
            namespace_id, collection_id, limit=limit, offset=offset, status=status
        )

    async def get(self, user: User, namespace_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        await self._authorizer.authorize(user, namespace_id)
        document = await self._documents.get(namespace_id, document_id)
        if document is None:
            raise NotFound("Document not found.")
        return document

    async def list_chunks(
        self,
        user: User,
        namespace_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[DocumentChunk], int]:
        document = await self.get(user, namespace_id, document_id)
        return await self._chunks.list_for_document(document.id, limit=limit, offset=offset)

    # --------------------------------------------------------------- delete ---
    async def delete(self, user: User, namespace_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        """Retire a document and hand its vector cleanup to a job.

        The database row is soft-deleted here, synchronously, and that is
        what makes the document unreachable: retrieval resolves every hit
        back through the database, so an un-purged vector cannot be surfaced
        or cited. Removing the vectors is therefore housekeeping, and
        housekeeping that can fail - the store may be down - so it belongs in
        a retryable job rather than in the user's request.

        The document is left in ``deleting`` rather than ``archived`` until
        that job succeeds, so a purge that never completed is visible instead
        of being indistinguishable from one that did. The caller enqueues the
        job; see :class:`~app.services.lifecycle.DocumentLifecycleService`.
        """
        context = await self._authorizer.authorize(user, namespace_id)
        document = await self._documents.get(namespace_id, document_id)
        if document is None:
            raise NotFound("Document not found.")

        # Uploaders may retire their own documents; removing someone else's
        # requires an admin.
        if document.uploaded_by_id != user.id:
            context.require(NamespaceRole.ADMIN)
        else:
            context.require_write()

        document.deleted_at = datetime.now(UTC)
        document.status = DocumentStatus.DELETING
        collection = await self._collections.get(namespace_id, document.collection_id)
        if collection is not None:
            collection.document_count = max(0, collection.document_count - 1)
            collection.chunk_count = max(0, collection.chunk_count - document.chunk_count)
        await self._documents.flush()

        await self._audit.record(
            action=AuditAction.DELETE,
            event="document.delete",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="document",
            resource_id=document_id,
            resource_label=document.filename,
        )
        return document
