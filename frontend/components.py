"""Shared rendering: statuses, citations, empty states.

Kept in one module because these are the pieces that must look the same
wherever they appear - a document's status means the same thing on the
upload page and in a chat sidebar, and a citation rendered two different
ways is a citation the user has to learn twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import streamlit as st

#: Icon and tone per document status. `queued` deliberately does not read as
#: a failure: it means a retry is already scheduled, and showing it in red
#: would send people looking for a problem that is being handled.
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "pending": ("⏳", "Accepted, not yet queued"),
    "queued": ("🕓", "Waiting for a worker"),
    "processing": ("⚙️", "Extracting and embedding"),
    "ready": ("✅", "Searchable"),
    "failed": ("⛔", "Needs attention"),
    "deleting": ("🧹", "Removing its vectors"),
    "archived": ("🗄️", "Deleted"),
}

JOB_STATUS_STYLE: dict[str, str] = {
    "queued": "🕓",
    "running": "⚙️",
    "succeeded": "✅",
    "failed": "⛔",
    "cancelled": "🚫",
}


def status_badge(status: str) -> str:
    icon, _ = STATUS_STYLE.get(status, ("•", status))
    return f"{icon} {status}"


def status_help(status: str) -> str:
    return STATUS_STYLE.get(status, ("", status))[1]


def when(value: str | None) -> str:
    """A timestamp as something a person reads at a glance."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    delta = (datetime.now(UTC) - moment).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return moment.strftime("%d %b %Y, %H:%M")


def empty_state(message: str, hint: str | None = None) -> None:
    st.info(message, icon=":material/inbox:")
    if hint:
        st.caption(hint)


def render_citations(citations: list[dict[str, Any]], *, key: str) -> None:
    """Show what an answer was based on.

    Expanded by default when there are few: the citations are the difference
    between an answer a user can check and one they have to trust, so hiding
    them behind a click undercuts the point of the system. The excerpt is the
    actual retrieved text, not a summary - a paraphrased source cannot be
    verified against the document.
    """
    if not citations:
        return

    label = f"Sources ({len(citations)})"
    with st.expander(label, expanded=len(citations) <= 3):
        for citation in citations:
            marker = citation.get("marker")
            name = citation.get("document_name") or "Untitled document"
            page = citation.get("page_number")
            # Page numbers are only shown where the format actually had one.
            # DOCX and plain text report none, and inventing "page 1" would
            # send the reader looking for something that does not exist.
            location = f" · page {page}" if page else ""
            score = citation.get("score")
            score_text = f" · score {score:.2f}" if isinstance(score, int | float) else ""

            st.markdown(f"**[{marker}] {name}**{location}{score_text}")
            excerpt = (citation.get("excerpt") or "").strip()
            if excerpt:
                st.markdown(
                    f"> {excerpt}".replace("\n", "\n> "),
                    help="The retrieved text, verbatim.",
                )
            st.divider()
    st.caption(
        "Every bracketed number above is resolved against the passages actually "
        "retrieved for this question.",
        help=f"citations-{key}",
    )


def answer_quality(done: dict[str, Any]) -> None:
    """A one-line verdict on how much weight the answer carries."""
    if not done.get("has_sufficient_evidence", True):
        st.warning(
            "The assistant could not find enough in these documents to answer.",
            icon=":material/help:",
        )
        return
    if done.get("truncated"):
        st.caption("⚠️ The answer hit the length limit and may be cut short.")
    if not done.get("grounded", True):
        st.caption("⚠️ This answer cites no passage — treat it with care.")
