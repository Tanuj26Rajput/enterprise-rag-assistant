"""ORM models.

Importing this package registers every model on ``Base.metadata``. Alembic's
``env.py`` imports it for exactly that reason: a model that is not imported is
invisible to autogenerate, and the next migration would silently drop its
table.
"""

from app.db.base import Base
from app.db.models.audit import AuditLog
from app.db.models.collection import Collection
from app.db.models.conversation import Conversation, Message
from app.db.models.document import Document, DocumentChunk
from app.db.models.enums import (
    AuditAction,
    AuditOutcome,
    DocumentStatus,
    JobKind,
    JobStatus,
    MessageRole,
    NamespaceRole,
)
from app.db.models.job import IngestionJob
from app.db.models.namespace import Namespace, NamespaceMember
from app.db.models.user import User

__all__ = [
    "AuditAction",
    "AuditLog",
    "AuditOutcome",
    "Base",
    "Collection",
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "IngestionJob",
    "JobKind",
    "JobStatus",
    "Message",
    "MessageRole",
    "Namespace",
    "NamespaceMember",
    "NamespaceRole",
    "User",
]
