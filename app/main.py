"""FastAPI application factory.

Note what is *not* here: there is no ``Base.metadata.create_all()`` on
startup. The schema belongs to Alembic (``alembic upgrade head``). Several
workers booting at once would otherwise race to issue DDL, and the running
schema would quietly drift away from the migration history.

**Three operational endpoints, and why they are three.**

``/health`` is liveness. It answers "is this process still working?" and
touches nothing outside it. An orchestrator restarts a container that fails
liveness, so a liveness probe that checks the database would restart every
replica during a database failover - turning a recoverable dependency outage
into a cluster-wide outage of its own.

``/ready`` is readiness. It checks the dependencies a request actually needs,
and a failure removes the replica from the load balancer *without* killing
it. That is the correct response to "the database is down": stop sending
traffic, keep the process alive so it can rejoin when the dependency comes
back.

``/metrics`` is the Prometheus exposition. It is unauthenticated, which is
deliberate and bounded: it carries counts and latencies labelled only with
route templates and configuration values - no identifiers, no payloads, no
query text. It is still an internal endpoint, and the deployment notes say to
keep it off the public listener.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import VectorStoreDep
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, UpstreamMisconfigured
from app.core.logging import configure_logging, request_id_var
from app.core.metrics import metrics, record_request
from app.db.session import check_database_connection, dispose_engine
from app.llm.base import LLMConfigurationError
from app.schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: A client may supply its own correlation id, which is genuinely useful when
#: a request crosses several services. It is only honoured if it looks like an
#: id: the value is written to logs and to audit rows, so an unvalidated one
#: lets a caller forge log lines with newlines, or bloat every row it touches.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Not metered, and not access-logged. A scrape every 15 seconds would
#: otherwise dominate both the request histogram it is reporting and the log
#: volume it is meant to help explain.
_UNMETERED = frozenset({"/metrics", "/health", "/ready"})


def _correlation_id(supplied: str | None) -> str:
    if supplied and _SAFE_REQUEST_ID.match(supplied):
        return supplied
    return uuid.uuid4().hex


def _route_template(request: Request, api_prefix: str = "") -> str:
    """The matched route's path template, for use as a metric and log label.

    The raw path is never acceptable here: ``/documents/<uuid>`` would mint a
    new time series per document, and the series are kept for the life of the
    process and stored forever by the scraper. An unmatched path collapses to
    a single ``<unmatched>`` series for the same reason - a 404 scanner must
    not be able to create series at will.

    The prefix has to be restored by hand. This FastAPI version keeps an
    included router nested rather than flattening its routes into the app, so
    ``route.path`` is relative to the prefix the router was mounted under and
    ``root_path`` is empty - a versioned route would otherwise report
    ``/namespaces/{namespace_id}``, a template that matches no URL this
    service serves and that collides with any future unversioned route of the
    same shape.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if not isinstance(path, str):
        return "<unmatched>"
    if api_prefix and not path.startswith(api_prefix) and request.url.path.startswith(api_prefix):
        return f"{api_prefix}{path}"
    return path


def _error_response(
    status_code: int,
    code: str,
    message: str,
    request: Request,
    details: object = None,
) -> JSONResponse:
    """Every failure leaves through here, in one shape."""
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
            request_id=getattr(request.state, "request_id", None),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers={REQUEST_ID_HEADER: getattr(request.state, "request_id", "")},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    logger.info(
        "Starting %s",
        settings.app_name,
        extra={
            "version": settings.app_version,
            "environment": settings.environment,
            "ingestion_mode": settings.ingestion_mode,
            "embedding_model": settings.embedding_spec.model,
            "embedding_dimensions": settings.embedding_spec.dimensions,
            "llm_model": settings.llm_model,
        },
    )

    if not await check_database_connection():
        # Logged, not fatal: a database that is briefly unreachable should not
        # stop the process from starting. It will fail /ready and stay out of
        # the load balancer until the dependency returns.
        logger.error("Database is unreachable at startup; /ready will report not-ready")

    yield

    logger.info("Shutting down; disposing the database engine")
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    # A build-info gauge is the conventional way to get version and
    # environment onto a dashboard: the value is always 1 and the labels
    # carry the information, so `rag_build_info` can be joined onto any other
    # series during a rollout to tell the two versions apart. Set here rather
    # than in `lifespan` because it describes the configuration this process
    # was constructed with, and because lifespan does not run under an
    # in-process ASGI client - which would make it absent in exactly the
    # tests that check the exposition.
    metrics.gauge(
        "rag_build_info",
        1,
        "Build and configuration identity; always 1, read the labels.",
        version=settings.app_version,
        environment=settings.environment,
        embedding_model=settings.embedding_spec.model,
        llm_model=settings.llm_model,
    )

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Enterprise multi-document RAG assistant.",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    api_prefix = settings.api_v1_prefix

    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        """Correlate, time and meter one request.

        The id is set both on ``request.state`` (so handlers and the error
        envelope can read it) and in a context variable (so every log line
        emitted anywhere beneath this call carries it without being passed a
        correlation id by hand).
        """
        request_id = _correlation_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handler below turns this into a 500 response, but
            # it runs *inside* `call_next`; an exception escaping to here is
            # one Starlette could not route, and it must still be metered or
            # the error rate silently under-reports.
            duration = time.perf_counter() - started
            if request.url.path not in _UNMETERED:
                record_request(request.method, _route_template(request, api_prefix), 500, duration)
            raise
        finally:
            request_id_var.reset(token)

        duration = time.perf_counter() - started
        response.headers[REQUEST_ID_HEADER] = request_id

        if request.url.path not in _UNMETERED:
            record_request(
                request.method,
                _route_template(request, api_prefix),
                response.status_code,
                duration,
            )
            logger.info(
                "%s %s %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={
                    # The path template, not the path: the raw one carries
                    # resource ids, and an access log is retained far longer
                    # and read far more widely than an audit row.
                    "route": _route_template(request, api_prefix),
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "request_id": request_id,
                },
            )
        return response

    # ----------------------------------------------------- error handlers ---
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.exception("Unhandled application error", exc_info=exc)
        return _error_response(exc.status_code, exc.code, exc.message, request, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's raw errors can echo submitted values, which for a login
        # payload would mean the password. Only the location and reason are
        # forwarded.
        details = [
            {"field": ".".join(str(part) for part in error["loc"][1:]), "error": error["msg"]}
            for error in exc.errors()
        ]
        return _error_response(
            422,  # named constant was renamed across Starlette versions
            "validation_failed",
            "The request payload failed validation.",
            request,
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 401: "authentication_failed"}
        return _error_response(
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(exc.detail),
            request,
        )

    @app.exception_handler(LLMConfigurationError)
    async def handle_llm_misconfigured(
        request: Request, exc: LLMConfigurationError
    ) -> JSONResponse:
        """A missing or malformed LLM key is 503, not 500.

        The answer service maps this itself, but only for errors raised
        inside a generation call. A provider built lazily can raise it
        from anywhere that first touches the model, and without this
        handler such a call would reach the catch-all below and be
        reported as an unexpected internal error - sending an operator
        looking for a bug instead of an unset variable.
        """
        logger.error("Chat provider is misconfigured: %s", exc)
        mapped = UpstreamMisconfigured()
        return _error_response(mapped.status_code, mapped.code, mapped.message, request)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The client gets a generic message; the detail goes to the logs.
        logger.exception("Unhandled exception", exc_info=exc)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
            request,
        )

    # -------------------------------------------------------- operations ---
    @app.get("/health", tags=["operations"], summary="Liveness probe")
    async def health() -> dict[str, object]:
        """Is this process alive? Nothing external is consulted.

        Always 200 while the event loop is running. A dependency check here
        would make a database failover restart every replica.
        """
        return {
            "status": "ok",
            "version": settings.app_version,
            "environment": settings.environment,
        }

    @app.get("/ready", tags=["operations"], summary="Readiness probe")
    async def ready(response: Response, vectors: VectorStoreDep) -> dict[str, object]:
        """Can this process serve a request right now?

        Both dependencies are probed on every call rather than cached. A
        cached readiness answer is a readiness answer that is wrong for the
        length of the cache, which is exactly the window during which traffic
        should have been drained.
        """
        database_ok = await check_database_connection()
        try:
            vectors_ok = await vectors.heartbeat()
        except Exception:
            logger.warning("Vector store readiness probe failed", exc_info=True)
            vectors_ok = False

        ready_now = database_ok and vectors_ok
        if not ready_now:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        metrics.gauge("rag_ready", 1 if ready_now else 0, "1 when the replica is serving traffic.")

        return {
            "status": "ready" if ready_now else "not_ready",
            "version": settings.app_version,
            "dependencies": {
                "database": "ok" if database_ok else "unreachable",
                "vector_store": "ok" if vectors_ok else "unreachable",
            },
        }

    @app.get("/metrics", tags=["operations"], summary="Prometheus metrics", include_in_schema=False)
    async def prometheus_metrics() -> PlainTextResponse:
        # version=0.0.4 in the content type is what makes a scraper parse this
        # as the text exposition format rather than guess.
        return PlainTextResponse(
            metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
