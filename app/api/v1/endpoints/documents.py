"""Document upload and inspection endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status

from app.api.deps import (
    CurrentUser,
    DocumentServiceDep,
    LifecycleServiceDep,
    PageParamsDep,
    SessionDep,
    SettingsDep,
)
from app.core.exceptions import ValidationFailed
from app.db.models import DocumentStatus, JobKind
from app.schemas.common import Page
from app.schemas.document import (
    DocumentChunkRead,
    DocumentRead,
    IngestionJobRead,
    ReconcileResult,
)

router = APIRouter(prefix="/namespaces/{namespace_id}", tags=["documents"])


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload, refusing to buffer more than the configured limit.

    Reading in chunks and stopping one byte past the limit means an oversized
    upload costs bounded memory, rather than being read in full and only then
    rejected.
    """
    buffer = bytearray()
    while chunk := await upload.read(64 * 1024):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ValidationFailed(
                "The file exceeds the maximum upload size.",
                details={"max_size_bytes": max_bytes},
            )
    return bytes(buffer)


@router.post(
    "/collections/{collection_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document (PDF, DOCX, TXT or Markdown)",
)
async def upload_document(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    lifecycle: LifecycleServiceDep,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="The document to ingest")],
    title: Annotated[
        str | None,
        Form(description="Optional display title", max_length=512),
    ] = None,
) -> DocumentRead:
    content = await _read_capped(file, settings.max_upload_size_bytes)
    document = await service.upload(
        user,
        namespace_id,
        collection_id,
        filename=file.filename,
        content_type=file.content_type,
        content=content,
        title=title,
    )
    # The job row is written before any work starts, in either mode. Under
    # INGESTION_MODE=inline it then runs here, so the response reports the
    # real outcome; under `background` the response returns with the document
    # `queued` and a worker picks it up. Either way a crash leaves a record
    # to recover from rather than an upload nobody will ever process.
    await lifecycle.enqueue_and_maybe_run(document, JobKind.INGEST)
    await session.commit()
    return DocumentRead.model_validate(document)


@router.get(
    "/collections/{collection_id}/documents",
    response_model=Page[DocumentRead],
    summary="List documents in a collection",
)
async def list_documents(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    page: PageParamsDep,
    status_filter: DocumentStatus | None = None,
) -> Page[DocumentRead]:
    documents, total = await service.list_for_collection(
        user,
        namespace_id,
        collection_id,
        limit=page.limit,
        offset=page.offset,
        status=status_filter,
    )
    return Page.build([DocumentRead.model_validate(d) for d in documents], total, page)


@router.get("/documents/{document_id}", response_model=DocumentRead, summary="Read a document")
async def read_document(
    namespace_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
) -> DocumentRead:
    document = await service.get(user, namespace_id, document_id)
    return DocumentRead.model_validate(document)


@router.get(
    "/documents/{document_id}/chunks",
    response_model=Page[DocumentChunkRead],
    summary="List a document's chunks",
)
async def list_chunks(
    namespace_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    page: PageParamsDep,
) -> Page[DocumentChunkRead]:
    chunks, total = await service.list_chunks(
        user, namespace_id, document_id, limit=page.limit, offset=page.offset
    )
    return Page.build([DocumentChunkRead.model_validate(c) for c in chunks], total, page)


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentRead,
    summary="Re-run extraction and chunking",
)
async def reprocess_document(
    namespace_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    lifecycle: LifecycleServiceDep,
    session: SessionDep,
) -> DocumentRead:
    """Queue a fresh ingestion attempt.

    This is also how a document whose job was dead-lettered gets another
    chance: the failed job stays as the record of what went wrong, and a new
    one starts with a full attempt budget. Safe to call on a document that is
    already ready - re-ingestion replaces its chunks and vectors in place.
    """
    document, _ = await lifecycle.reprocess(user, namespace_id, document_id)
    await session.commit()
    return DocumentRead.model_validate(document)


@router.get(
    "/documents/{document_id}/jobs",
    response_model=list[IngestionJobRead],
    summary="A document's processing history",
)
async def list_document_jobs(
    namespace_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    lifecycle: LifecycleServiceDep,
) -> list[IngestionJobRead]:
    jobs = await lifecycle.jobs_for_document(user, namespace_id, document_id)
    return [IngestionJobRead.model_validate(job) for job in jobs]


@router.post(
    "/collections/{collection_id}/reconcile",
    response_model=ReconcileResult,
    summary="Remove vectors with no live chunk behind them (admin)",
)
async def reconcile_collection(
    namespace_id: uuid.UUID,
    collection_id: uuid.UUID,
    user: CurrentUser,
    lifecycle: LifecycleServiceDep,
    session: SessionDep,
) -> ReconcileResult:
    """Sweep a collection's index for orphaned vectors.

    Orphans are never a correctness problem - retrieval resolves every hit
    through the database, so a vector without a live chunk cannot be returned
    or cited. They are a size and cost problem, which is why this is an
    explicit admin operation rather than something the request path pays for.
    """
    removed = await lifecycle.reconcile(user, namespace_id, collection_id)
    await session.commit()
    return ReconcileResult(collection_id=collection_id, vectors_removed=removed)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
)
async def delete_document(
    namespace_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    service: DocumentServiceDep,
    lifecycle: LifecycleServiceDep,
    session: SessionDep,
) -> None:
    """Retire a document; its vectors are purged by a retryable job.

    The soft delete is what makes the document unreachable, and it is
    committed here. Vector removal is queued rather than attempted inline so
    that a vector store outage cannot fail - or silently half-finish - a
    delete the user is entitled to.
    """
    document = await service.delete(user, namespace_id, document_id)
    await lifecycle.enqueue_and_maybe_run(document, JobKind.DELETE)
    await session.commit()
