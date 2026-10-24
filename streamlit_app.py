"""Single-process entrypoint for Streamlit Community Cloud.

Streamlit Cloud runs one process on one exposed port, so there is nowhere to
deploy the FastAPI backend separately. This module starts the backend as a
background subprocess bound to loopback, waits for it to come up, then
renders the Streamlit frontend in the foreground exactly as
``frontend/app.py`` does for every other deployment (docker-compose, or
``streamlit run frontend/app.py`` against a backend started on its own).
That file is unchanged and still works on its own - this just wraps it.

Point Streamlit Cloud's "Main file path" at this file instead of
``frontend/app.py``. Everything the backend needs (``DATABASE_URL``,
``SECRET_KEY``, the LLM/embedding provider keys - see ``app/core/config.py``)
goes in the app's Secrets, in ``KEY = "value"`` TOML form; this module copies
them into the process environment before anything imports settings.

Two deployment limitations worth knowing, inherent to a single free-tier
container rather than to this file:

* The local Chroma store (``./var/chroma``) lives on the container's
  ephemeral disk and is wiped whenever Streamlit Cloud restarts or reboots
  the app. Point ``CHROMA_HOST``/``CHROMA_PORT`` at a hosted Chroma instance
  for data that needs to survive a restart.
* ``DATABASE_URL`` must point at a database reachable from the internet
  (e.g. a free Supabase or Neon Postgres) - Streamlit Cloud has no database
  of its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_BACKEND_PORT = 8000
_BACKEND_URL = f"http://127.0.0.1:{_BACKEND_PORT}"


def _export_secrets() -> None:
    """Copy Streamlit Cloud secrets into the environment.

    ``st.secrets`` is how Streamlit Cloud delivers configuration; the backend
    settings (``app.core.config.Settings``) only ever read ``os.environ`` or
    a local ``.env``. ``setdefault`` so a real environment variable - set by
    docker-compose, or a local ``.env`` when running this file for a quick
    check outside Streamlit Cloud - always wins over a secret of the same
    name.
    """
    try:
        secrets = st.secrets
    except Exception:
        return
    for key in secrets.keys():
        os.environ.setdefault(str(key), str(secrets[key]))


def _wait_until_ready(timeout: float = 60.0) -> bool:
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{_BACKEND_URL}/health", timeout=2).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


@st.cache_resource(show_spinner="Starting the API...")
def _start_backend() -> bool:
    """Run migrations once and start the API as a background subprocess.

    ``st.cache_resource`` makes this run exactly once per process - Streamlit
    reruns this whole module on every user interaction, and this must not
    start a second backend, or a second migration, on every click.
    """
    os.environ.setdefault("RAG_API_URL", _BACKEND_URL)
    os.environ.setdefault("HOST", "127.0.0.1")
    os.environ.setdefault("PORT", str(_BACKEND_PORT))

    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    if migrate.returncode != 0:
        print(migrate.stdout, file=sys.stderr)
        print(migrate.stderr, file=sys.stderr)
        st.error("Database migration failed. Check the app logs for details.")
        return False

    # `run.py`, not `uvicorn app.main:app` directly: it carries the Windows
    # event-loop workaround needed for psycopg's async driver, and reads
    # host/port/reload from the same settings the rest of the app uses.
    subprocess.Popen([sys.executable, "run.py"], cwd=str(_ROOT))

    if not _wait_until_ready():
        st.error("The API did not become ready in time. Check the app logs for details.")
        return False
    return True


_export_secrets()

if not _start_backend():
    st.stop()

os.environ.setdefault("RAG_API_URL", _BACKEND_URL)

import frontend.app  # noqa: E402

# Streamlit re-executes *this* file on every rerun, but a module import only
# runs `frontend/app.py`'s top level once - its own `main()` call is guarded
# by `if __name__ == "__main__"` for exactly this reason. Call it explicitly
# every time so the page actually re-renders on each interaction.
frontend.app.main()
