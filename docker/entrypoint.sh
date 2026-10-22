#!/bin/sh
# Role selector for the shared image: `api`, `worker`, `frontend` or `migrate`.
#
# `set -e` is load-bearing. Without it a failed `alembic upgrade` would be
# followed by uvicorn starting anyway, and the first request would fail
# against a schema that is one migration behind - a confusing symptom a long
# way from its cause.
set -eu

role="${1:-api}"
shift || true

# `exec` replaces this shell with the application process, so the application
# becomes PID 1 and receives SIGTERM directly. Without it the shell is PID 1,
# does not forward signals, and every deploy ends in a 10-second timeout and
# a SIGKILL - which for the worker means a job killed mid-flight rather than
# a lease released cleanly.
case "$role" in
  api)
    # Migrations are deliberately NOT run here. Several API replicas starting
    # together would race, and a schema change would be applied by whichever
    # container happened to win. Run the `migrate` role once, as its own step.
    exec uvicorn app.main:app \
      --host "${HOST:-0.0.0.0}" \
      --port "${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-1}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
      --no-access-log \
      "$@"
    ;;

  worker)
    exec python -m app.worker "$@"
    ;;

  frontend)
    exec streamlit run frontend/app.py \
      --server.address "${HOST:-0.0.0.0}" \
      --server.port "${STREAMLIT_PORT:-8501}" \
      --server.headless true \
      --browser.gatherUsageStats false \
      "$@"
    ;;

  migrate)
    exec alembic upgrade head
    ;;

  *)
    # Anything else is run verbatim, so `docker compose run api sh` and
    # `... python -m app.worker --help` still work for debugging.
    exec "$role" "$@"
    ;;
esac
