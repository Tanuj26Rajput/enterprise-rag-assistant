"""Streamlit entrypoint.

    streamlit run frontend/app.py

This process holds no credentials of its own. It knows one URL, it attaches
the signed-in user's bearer token to every call, and the backend decides what
that token is allowed to see. There is no database driver imported anywhere
under `frontend/`, and no model client - a quick `grep` for `psycopg` or
`openai` in this package returns nothing, which is the intended property.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run frontend/app.py` puts *this* directory on sys.path, not the
# repository root, so `import frontend.…` would fail depending on where the
# command was issued from. Two lines here make the app runnable from any
# working directory, which is what a container's ENTRYPOINT needs.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# E402 below is the price of that bootstrap: these imports cannot run until
# the path is set up, which is the one case where import order is load-bearing.
import streamlit as st  # noqa: E402

from frontend.config import warn_about_leaked_secrets  # noqa: E402
from frontend.state import (  # noqa: E402
    current_namespace,
    is_signed_in,
    select_namespace,
    sign_out,
)
from frontend.views import auth, chat, documents, search, workspaces  # noqa: E402

PAGES = {
    "Chat": (chat.render, ":material/forum:"),
    "Documents": (documents.render, ":material/description:"),
    "Search": (search.render, ":material/search:"),
    "Workspaces": (workspaces.render, ":material/workspaces:"),
}


def main() -> None:
    # Once per process, to the log. A shared `.env` handing this process the
    # database password is a deployment mistake worth reporting, but to the
    # person who can fix it - not to every end user, who cannot.
    warn_about_leaked_secrets()

    st.set_page_config(
        page_title="Enterprise RAG Assistant",
        page_icon="📚",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    if not is_signed_in():
        # Every page is behind this check, so there is no route that renders
        # workspace data without a token. The backend would refuse anyway;
        # this is about not showing an empty shell that looks broken.
        auth.render()
        return

    page = _sidebar()
    render, _ = PAGES[page]
    render()


def _sidebar() -> str:
    from frontend.state import client, guard, items

    with st.sidebar:
        user = st.session_state.get("user", {})
        st.markdown(f"**{user.get('full_name') or user.get('email', 'Signed in')}**")
        st.caption(user.get("email", ""))

        namespaces = items(guard(client().list_namespaces))
        if namespaces:
            current = current_namespace()
            index = next(
                (i for i, n in enumerate(namespaces) if current and n["id"] == current["id"]),
                0,
            )
            chosen = st.selectbox(
                "Workspace",
                namespaces,
                index=index,
                format_func=lambda n: f"{n['name']} ({n.get('role', 'member')})",
            )
            select_namespace(chosen)
        else:
            select_namespace(None)
            st.caption("No workspaces yet — create one on the Workspaces page.")

        st.divider()
        page = st.radio(
            "Go to",
            list(PAGES),
            label_visibility="collapsed",
            captions=None,
            key="page",
        )
        st.divider()

        if st.button("Sign out", use_container_width=True, icon=":material/logout:"):
            sign_out()
            st.rerun()

    return page


main()
