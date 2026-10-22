"""Blob storage for uploaded documents.

Only a local filesystem backend exists today. It is behind a small interface
so the production deployment can swap in S3 or Supabase Storage without the
document service changing: the database stores an opaque ``storage_uri``, not
a path.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol


class BlobStorage(Protocol):
    """Where document bytes live."""

    async def put(
        self, *, namespace_id: uuid.UUID, document_id: uuid.UUID, extension: str, content: bytes
    ) -> str:
        """Store the bytes and return a URI that :meth:`get` accepts."""
        ...

    async def get(self, uri: str) -> bytes: ...

    async def delete(self, uri: str) -> None: ...


class LocalFileStorage:
    """Filesystem-backed storage, partitioned by namespace."""

    scheme = "file"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def _path_for(self, namespace_id: uuid.UUID, document_id: uuid.UUID, extension: str) -> Path:
        # The document UUID is the filename: the client-supplied name is kept
        # in the database but never used to build a path.
        return self._root / str(namespace_id) / f"{document_id}{extension}"

    def _resolve(self, uri: str) -> Path:
        if not uri.startswith(f"{self.scheme}://"):
            raise ValueError(f"Unsupported storage URI: {uri!r}")
        path = Path(uri[len(self.scheme) + 3 :]).resolve()
        # Refuse to read or delete anything outside the storage root, however
        # the URI was constructed.
        if not path.is_relative_to(self._root):
            raise ValueError("Storage URI escapes the storage root.")
        return path

    async def put(
        self, *, namespace_id: uuid.UUID, document_id: uuid.UUID, extension: str, content: bytes
    ) -> str:
        path = self._path_for(namespace_id, document_id, extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"{self.scheme}://{path}"

    async def get(self, uri: str) -> bytes:
        return self._resolve(uri).read_bytes()

    async def delete(self, uri: str) -> None:
        self._resolve(uri).unlink(missing_ok=True)
