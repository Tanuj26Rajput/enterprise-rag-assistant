"""Structured logging, and the request id that threads through it.

Two decisions are worth explaining.

**JSON by default, text when a human is watching.** A log line is read by a
machine far more often than by a person: once it is in Loki, CloudWatch or
Datadog, ``request_id="7f3a"`` is a filter and ``[7f3a]`` is a substring
search that also matches a UUID that merely contains those characters. The
console renderer exists for ``LOG_JSON=false`` during local development,
where the reader is a person and a wall of JSON is worse than useless.

**The request id travels in a ``ContextVar``, not in every signature.** The
alternative is threading a correlation id through services, repositories and
the vector store - a parameter that exists only for logging, on functions
that otherwise have no opinion about it. A context variable is set once per
request and is visible to every log call underneath it, including inside
``asyncio.to_thread``, because ``contextvars`` are copied into the worker.

Only the standard library is used. ``structlog`` would be a reasonable
dependency, but everything needed here is a ``Formatter`` and a ``Filter``,
and a dependency that is one file of code is a dependency that is mostly
risk.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

#: Set by the HTTP middleware (and by the worker, per job). Empty outside a
#: request, which is correct: startup and shutdown lines belong to no call.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

#: Set by the worker so every line a job emits names the job. The HTTP path
#: leaves it empty.
job_id_var: ContextVar[str] = ContextVar("job_id", default="")

#: ``LogRecord`` attributes that exist on every record. Anything else a caller
#: passed through ``extra=`` is application data and is merged into the JSON
#: object, which is what makes ``logger.info("...", extra={"document_id": x})``
#: produce a queryable field rather than a buried string.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"asctime", "message", "taskName"}
)


class ContextFilter(logging.Filter):
    """Copy the ambient correlation ids onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.job_id = job_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with no interleaved multi-line tracebacks.

    A traceback is carried as a single ``exception`` string rather than
    printed across many lines: a multi-line log record is one record to a
    human and twenty unrelated records to a log shipper, which is how
    tracebacks end up split across different search results.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := getattr(record, "request_id", ""):
            payload["request_id"] = request_id
        if job_id := getattr(record, "job_id", ""):
            payload["job_id"] = job_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        # `default=str` rather than a custom encoder: a UUID, a Decimal or a
        # datetime in `extra=` should log, not raise. Losing type fidelity in
        # a log line is an acceptable trade; losing the line is not.
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable, with the correlation id kept short enough to scan."""

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", "")
        prefix = f"[{request_id[:8]}] " if request_id else ""
        base = super().format(record)
        return f"{prefix}{base}"


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the root handler. Idempotent, so repeated calls are harmless.

    uvicorn installs its own handlers on ``uvicorn.access`` and
    ``uvicorn.error``; they are cleared and left to propagate so that *all*
    output goes through one formatter. Otherwise half the lines are JSON and
    half are uvicorn's own format, which defeats the point.
    """
    formatter: logging.Formatter = (
        JsonFormatter()
        if json_output
        else ConsoleFormatter("%(levelname)-8s %(name)s: %(message)s")
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # Chatty at INFO and uninteresting unless something is wrong: SQLAlchemy
    # echoes every statement, httpx logs every outbound call, and Chroma's
    # telemetry module logs on import.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)
