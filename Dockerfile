# syntax=docker/dockerfile:1.7
#
# One image, three roles. The API, the ingestion worker and the Streamlit
# frontend all run the same code and the same dependency set; which one a
# container becomes is decided by its command, not by a separate build. Three
# images would triple the build time and introduce the possibility of a
# worker running different code than the API that enqueued its jobs - a class
# of bug that is miserable to diagnose and impossible to reproduce locally.
#
# Two stages. The builder has the compilers; the runtime has the wheels. The
# runtime image therefore carries no gcc, no build headers and no pip cache,
# which is both smaller and a materially smaller attack surface.

# ----------------------------------------------------------------- builder ---
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential is needed by any dependency without a manylinux wheel for
# this platform; on arm64 that is several of them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than --user or the system site-packages: it is a single
# directory to copy into the runtime stage, with nothing else coming along.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies before source, so editing application code does not invalidate
# the layer that takes minutes to build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ----------------------------------------------------------------- runtime ---
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Containers log to stdout and a collector parses them, so JSON is the
    # default here even though it is awkward to read by hand.
    LOG_JSON=true \
    # 127.0.0.1 would bind to the container's own loopback and refuse every
    # connection from outside it. This is the one place where binding to all
    # interfaces is right: the container *is* the isolation boundary.
    HOST=0.0.0.0 \
    PORT=8000

# curl is the healthcheck. It is the only addition to the base image, and it
# is here rather than a Python one-liner because HEALTHCHECK runs on a short
# timeout and starting an interpreter is most of that budget.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# A non-root user with no login shell and no home to write to. If an upload
# parser is ever exploited, the process it lands in cannot write to /app,
# cannot install anything, and cannot escalate through a setuid binary it has
# no permission to run.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app/ ./app/
COPY --chown=app:app frontend/ ./frontend/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app alembic.ini run.py ./
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The two paths the application writes to. Both are declared volumes: the
# uploaded originals are the only copy of a user's file, and the Chroma
# directory is a database. Either one left on the container filesystem is
# deleted by the next `docker compose up --force-recreate`, silently.
RUN mkdir -p /data/uploads /data/chroma && chown -R app:app /data
ENV UPLOAD_DIR=/data/uploads \
    CHROMA_PERSIST_DIR=/data/chroma
VOLUME ["/data"]

USER app
EXPOSE 8000

# Liveness, not readiness: an orchestrator restarts what fails this, and a
# replica should not be restarted because its database is briefly away.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:${PORT}/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
