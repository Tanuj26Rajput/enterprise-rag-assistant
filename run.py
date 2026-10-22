"""Development entrypoint for Windows.

psycopg's async driver cannot run on Windows' ``ProactorEventLoop``:

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
    in async mode.

uvicorn picks the loop through a factory rather than the asyncio policy
(``uvicorn/loops/asyncio.py``: ProactorEventLoop on win32 unless it is running
the server in a subprocess), so ``asyncio.set_event_loop_policy`` has no
effect on it. The loop therefore has to be supplied explicitly, which is what
``main`` does below.

On Linux - including the container image - ``uvicorn app.main:app`` works
directly and this module is unnecessary.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.core.config import get_settings

APP = "app.main:app"


def main() -> None:
    settings = get_settings()

    if settings.reload:
        # The reloader runs the application in a subprocess, for which uvicorn
        # already selects a selector loop on Windows, so this path is safe.
        uvicorn.run(APP, host=settings.host, port=settings.port, reload=True)
        return

    server = uvicorn.Server(uvicorn.Config(APP, host=settings.host, port=settings.port))

    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.serve())
    else:
        server.run()


if __name__ == "__main__":
    main()
