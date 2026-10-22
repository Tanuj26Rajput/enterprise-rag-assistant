"""Enumerations persisted as native PostgreSQL enum types.

Native enums (rather than ``VARCHAR`` + check constraint) give us type safety
at the database level and keep the values readable in ad-hoc SQL. The cost is
that adding a value requires a migration - acceptable here, since each of these
sets changes rarely and a new value usually implies new application logic.
"""

from __future__ import annotations

import enum

import sqlalchemy as sa


class NamespaceRole(enum.StrEnum):
    """A member's authority inside one namespace.

    Ordered from most to least privileged; see :meth:`outranks`.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _NAMESPACE_ROLE_RANK[self]

    def outranks(self, other: NamespaceRole) -> bool:
        return self.rank < other.rank

    def can_write(self) -> bool:
        return self is not NamespaceRole.VIEWER


_NAMESPACE_ROLE_RANK = {
    NamespaceRole.OWNER: 0,
    NamespaceRole.ADMIN: 1,
    NamespaceRole.MEMBER: 2,
    NamespaceRole.VIEWER: 3,
}


class DocumentStatus(enum.StrEnum):
    """Lifecycle of an uploaded document.

    ``PENDING`` means accepted and stored but not yet claimed by a worker;
    ``QUEUED`` means a job exists for it and is waiting its turn. The two are
    distinguished because the difference matters when something goes wrong:
    a PENDING document with no job row is a lost upload, while a QUEUED one
    is merely waiting.

    ``DELETING`` is a tombstone for the window between "the user asked for
    this gone" and "its vectors are actually gone". Without it, a deletion
    interrupted half way leaves a READY document whose vectors are partly
    removed, and nothing to tell a sweeper that it should finish the job.
    """

    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    ARCHIVED = "archived"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DocumentStatus.READY,
            DocumentStatus.FAILED,
            DocumentStatus.ARCHIVED,
        }

    @property
    def is_in_flight(self) -> bool:
        """Work is expected to happen, or is happening, for this document."""
        return self in {
            DocumentStatus.PENDING,
            DocumentStatus.QUEUED,
            DocumentStatus.PROCESSING,
            DocumentStatus.DELETING,
        }


class MessageRole(enum.StrEnum):
    """Author of a chat message, mirroring the OpenAI chat schema."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AuditAction(enum.StrEnum):
    """Coarse category of an audited operation.

    The specific operation is carried in ``audit_logs.action``; this column
    exists so dashboards can group and filter without parsing strings.
    """

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"
    QUERY = "query"
    EXPORT = "export"


class AuditOutcome(enum.StrEnum):
    """Whether the audited operation succeeded."""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


def pg_enum(enum_class: type[enum.Enum], name: str) -> sa.Enum:
    """Build a native PostgreSQL enum that stores the member *values*.

    Without ``values_callable`` SQLAlchemy stores member *names* (``OWNER``),
    which would not match the lowercase values used throughout the API.
    """
    return sa.Enum(
        enum_class,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class JobKind(enum.StrEnum):
    """What a queued unit of work will do."""

    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"


class JobStatus(enum.StrEnum):
    """Lifecycle of one queued unit of work.

    ``FAILED`` is terminal and means "retries exhausted": the row stays as a
    dead letter so an operator can see what happened and requeue it, rather
    than the work disappearing silently.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


NAMESPACE_ROLE_ENUM = "namespace_role"
DOCUMENT_STATUS_ENUM = "document_status"
MESSAGE_ROLE_ENUM = "message_role"
AUDIT_ACTION_ENUM = "audit_action"
AUDIT_OUTCOME_ENUM = "audit_outcome"
JOB_KIND_ENUM = "job_kind"
JOB_STATUS_ENUM = "job_status"
