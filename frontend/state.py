"""Per-session state, and the helpers every view shares.

Streamlit re-runs the whole script on every interaction, so anything that
must survive a click lives in ``st.session_state`` - which is per browser
session, not per process. That is what makes a single Streamlit server safe
for several users: two people signed in at once have two token sets, and
neither can reach the other's.

Nothing here is cached globally. ``st.cache_data`` would be tempting for the
namespace list, but a cache keyed only by arguments would serve one user's
namespaces to another, which is exactly the cross-tenant leak the backend
works to prevent. Lists are small and the API is local; correctness wins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

from frontend.api import ApiClient, ApiError
from frontend.config import FrontendSettings, load_settings


def settings() -> FrontendSettings:
    if "settings" not in st.session_state:
        st.session_state.settings = load_settings()
    return st.session_state.settings


def client() -> ApiClient:
    if "client" not in st.session_state:
        st.session_state.client = ApiClient(settings())
    return st.session_state.client


def is_signed_in() -> bool:
    return bool(st.session_state.get("user")) and client().is_signed_in


def sign_out() -> None:
    """Drop every trace of the session, not just the token.

    Clearing the selections too, because leaving a namespace id behind would
    show the next person to sign in on this browser session a view scoped to
    somebody else's workspace - which the API would then refuse, producing a
    confusing error instead of a clean start.
    """
    client().sign_out()
    for key in ("user", "namespace", "collection", "conversation_id", "pending_question"):
        st.session_state.pop(key, None)


def current_namespace() -> dict[str, Any] | None:
    return st.session_state.get("namespace")


def require_namespace() -> dict[str, Any] | None:
    """The selected workspace, or a prompt to pick one."""
    namespace = current_namespace()
    if namespace is None:
        st.info("Select a workspace in the sidebar to continue.")
        return None
    return namespace


def select_namespace(namespace: dict[str, Any] | None) -> None:
    previous = st.session_state.get("namespace")
    st.session_state.namespace = namespace
    if previous is None or namespace is None or previous.get("id") != namespace.get("id"):
        # A collection or conversation id only means anything inside the
        # namespace it came from. Carrying one across would ask the API for
        # another tenant's resource and get a 404 - correct, but baffling.
        st.session_state.pop("collection", None)
        st.session_state.pop("conversation_id", None)


def select_collection(collection: dict[str, Any] | None) -> None:
    previous = st.session_state.get("collection")
    st.session_state.collection = collection
    if previous is None or collection is None or previous.get("id") != collection.get("id"):
        st.session_state.pop("conversation_id", None)


# ------------------------------------------------------------------ calling ---
def guard[T](call: Callable[[], T], *, spinner: str | None = None) -> T | None:
    """Run an API call, rendering any failure instead of raising.

    Returns None on failure, so a view reads as "fetch, then render if we got
    something". A traceback on screen would leak file paths and tell the user
    nothing they can act on.
    """
    try:
        if spinner:
            with st.spinner(spinner):
                return call()
        return call()
    except ApiError as exc:
        render_error(exc)
        if exc.status == 401:
            sign_out()
            st.rerun()
        return None


def render_error(exc: ApiError) -> None:
    """Show a failure the way the user can act on it.

    The request id is included where the API gave one: it is the thread
    between "it broke for me at 3pm" and the matching audit row and log line.
    """
    message = exc.message
    if exc.code == "unreachable":
        st.error(message, icon=":material/cloud_off:")
        st.caption("Start the API with `uvicorn app.main:app`, or set `RAG_API_URL`.")
        return
    if exc.status == 403:
        st.warning(message, icon=":material/lock:")
        return
    if exc.status == 404:
        st.warning(message, icon=":material/search_off:")
        return

    st.error(message, icon=":material/error:")
    if exc.details:
        with st.expander("Details"):
            st.json(exc.details)
    if exc.request_id:
        st.caption(f"Request ID `{exc.request_id}` — quote this when reporting the problem.")


def items(page: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The rows out of a paginated response, or an empty list."""
    if not page:
        return []
    return list(page.get("items") or [])
