"""Vector storage behind a backend-agnostic interface."""

from app.vectorstore.base import (
    META_CHUNK_ID,
    META_CHUNK_INDEX,
    META_COLLECTION_ID,
    META_CONTENT_HASH,
    META_DOCUMENT_ID,
    META_NAMESPACE_ID,
    META_PAGE_NUMBER,
    VectorCollectionMismatch,
    VectorMatch,
    VectorRecord,
    VectorStore,
    VectorStoreError,
    vector_id_for,
)
from app.vectorstore.chroma import ChromaVectorStore

__all__ = [
    "META_CHUNK_ID",
    "META_CHUNK_INDEX",
    "META_COLLECTION_ID",
    "META_CONTENT_HASH",
    "META_DOCUMENT_ID",
    "META_NAMESPACE_ID",
    "META_PAGE_NUMBER",
    "ChromaVectorStore",
    "VectorCollectionMismatch",
    "VectorMatch",
    "VectorRecord",
    "VectorStore",
    "VectorStoreError",
    "vector_id_for",
]
