"""Upload validation.

A file is accepted only when its extension, its declared MIME type and its
actual bytes all agree. Checking the extension or the ``Content-Type`` header
alone is not validation: both are attacker-controlled, and a parser handed a
file it does not expect is where document pipelines get exploited.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO

from app.core.exceptions import ValidationFailed


@dataclass(frozen=True, slots=True)
class FileKind:
    """A supported upload format."""

    name: str
    mime_type: str
    extensions: frozenset[str]


PDF = FileKind("pdf", "application/pdf", frozenset({".pdf"}))
DOCX = FileKind(
    "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    frozenset({".docx"}),
)
TXT = FileKind("txt", "text/plain", frozenset({".txt", ".text"}))
MARKDOWN = FileKind("markdown", "text/markdown", frozenset({".md", ".markdown"}))

SUPPORTED_KINDS: tuple[FileKind, ...] = (PDF, DOCX, TXT, MARKDOWN)
SUPPORTED_EXTENSIONS = frozenset(ext for kind in SUPPORTED_KINDS for ext in kind.extensions)

#: Every common filesystem stops at 255, and the column stops at 512.
MAX_FILENAME_LENGTH = 255

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
# A .docx is an OPC package; this part is what distinguishes it from .xlsx,
# .pptx or any other zip someone renamed.
_DOCX_REQUIRED_PART = "word/document.xml"


def _detect_kind(content: bytes, extension: str) -> FileKind:
    """Identify the format from the bytes themselves."""
    if content.startswith(_PDF_MAGIC):
        return PDF

    if content.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise ValidationFailed("The file is not a readable DOCX archive.") from exc
        if _DOCX_REQUIRED_PART not in names:
            raise ValidationFailed(
                "The file is a ZIP archive but not a Word document.",
                details={"expected_part": _DOCX_REQUIRED_PART},
            )
        return DOCX

    # Anything left must be text. Requiring a clean UTF-8 decode rejects
    # binaries with no recognisable header rather than storing them as "text".
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed(
            "The file is not a supported document. Expected PDF, DOCX or UTF-8 text."
        ) from exc

    # A NUL byte in a successful UTF-8 decode still means binary data.
    if "\x00" in decoded:
        raise ValidationFailed("The file contains binary data and is not valid text.")

    return MARKDOWN if extension in MARKDOWN.extensions else TXT


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """The result of validating an upload."""

    filename: str
    extension: str
    kind: FileKind
    content: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def validate_upload(
    *,
    filename: str | None,
    declared_mime_type: str | None,
    content: bytes,
    max_size_bytes: int,
    allowed_mime_types: set[str] | None = None,
) -> ValidatedUpload:
    """Validate an uploaded file, or raise :class:`ValidationFailed`."""
    if not filename or not filename.strip():
        raise ValidationFailed("A filename is required.")

    # Defend against path traversal in the client-supplied name before it is
    # ever used to build a storage path.
    safe_name = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not safe_name or safe_name in {".", ".."}:
        raise ValidationFailed("The filename is not valid.")

    # Bounded here rather than truncated later: the column is 512 characters,
    # and without this a longer name reaches the INSERT and fails there - a
    # 500 for what is plainly a bad request. 255 is the limit every common
    # filesystem imposes anyway, so nothing legitimate is being refused.
    if len(safe_name) > MAX_FILENAME_LENGTH:
        raise ValidationFailed(
            f"The filename is too long; the limit is {MAX_FILENAME_LENGTH} characters.",
            details={"length": len(safe_name), "max_length": MAX_FILENAME_LENGTH},
        )

    extension = ("." + safe_name.rsplit(".", 1)[-1].lower()) if "." in safe_name else ""
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValidationFailed(
            "Unsupported file type.",
            details={"extension": extension or None, "supported": sorted(SUPPORTED_EXTENSIONS)},
        )

    if not content:
        raise ValidationFailed("The file is empty.")

    # Size is checked against the bytes actually received, not against the
    # Content-Length header, which a client can understate.
    if len(content) > max_size_bytes:
        raise ValidationFailed(
            "The file exceeds the maximum upload size.",
            details={"size_bytes": len(content), "max_size_bytes": max_size_bytes},
        )

    kind = _detect_kind(content, extension)

    if extension not in kind.extensions:
        raise ValidationFailed(
            "The file contents do not match its extension.",
            details={"extension": extension, "detected": kind.name},
        )

    if allowed_mime_types is not None and kind.mime_type not in allowed_mime_types:
        raise ValidationFailed(
            "This file type is not permitted by the server configuration.",
            details={"detected": kind.mime_type},
        )

    # The declared type is advisory: it is checked for consistency but the
    # detected type is what gets stored.
    if declared_mime_type:
        declared = declared_mime_type.split(";", 1)[0].strip().lower()
        text_kinds = {TXT.mime_type, MARKDOWN.mime_type, "application/octet-stream"}
        consistent = declared == kind.mime_type or (
            kind in (TXT, MARKDOWN) and declared in text_kinds
        )
        if not consistent and declared != "application/octet-stream":
            raise ValidationFailed(
                "The declared content type does not match the file contents.",
                details={"declared": declared, "detected": kind.mime_type},
            )

    return ValidatedUpload(filename=safe_name, extension=extension, kind=kind, content=content)
