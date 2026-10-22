"""Upload documents and watch them become searchable.

The processing status is the part worth getting right. A document can be
waiting, being worked on, already retried twice, or given up on, and those
are four different things for the person who uploaded it - the first three
need patience and the fourth needs action. The backend distinguishes them, so
this page does too rather than collapsing everything into a spinner.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.components import JOB_STATUS_STYLE, empty_state, status_badge, status_help, when
from frontend.state import client, guard, items, require_namespace, select_collection

ACCEPTED_TYPES = ["pdf", "docx", "txt", "md"]
MIME_BY_SUFFIX = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}
IN_FLIGHT = {"pending", "queued", "processing", "deleting"}


def render() -> None:
    st.title("Documents")

    namespace = require_namespace()
    if namespace is None:
        return

    collections = items(guard(lambda: client().list_collections(namespace["id"])))
    if not collections:
        empty_state(
            "This workspace has no collections yet.",
            "Create one on the Workspaces page before uploading.",
        )
        return

    collection = _collection_picker(collections)
    if collection is None:
        return

    _upload(namespace, collection)
    st.divider()
    _document_list(namespace, collection)


def _collection_picker(collections: list[dict[str, Any]]) -> dict[str, Any] | None:
    current = st.session_state.get("collection")
    index = next((i for i, c in enumerate(collections) if current and c["id"] == current["id"]), 0)
    chosen = st.selectbox(
        "Collection",
        collections,
        index=index,
        format_func=lambda c: f"{c['name']} ({c['document_count']} documents)",
    )
    select_collection(chosen)
    return chosen


def _upload(namespace: dict[str, Any], collection: dict[str, Any]) -> None:
    uploaded = st.file_uploader(
        "Add documents",
        type=ACCEPTED_TYPES,
        accept_multiple_files=True,
        help="PDF, DOCX, TXT or Markdown. The file's actual contents are checked against "
        "its extension, so a renamed file is rejected.",
    )
    if not uploaded:
        return

    if not st.button("Upload", type="primary", icon=":material/upload:"):
        return

    progress = st.progress(0.0, text="Starting…")
    succeeded, failed = [], []

    for index, file in enumerate(uploaded, start=1):
        progress.progress(
            (index - 1) / len(uploaded), text=f"Uploading {file.name} ({index}/{len(uploaded)})…"
        )
        suffix = file.name.rsplit(".", 1)[-1].lower()
        document = guard(
            lambda file=file, suffix=suffix: client().upload_document(
                namespace["id"],
                collection["id"],
                filename=file.name,
                content=file.getvalue(),
                content_type=MIME_BY_SUFFIX.get(suffix, "application/octet-stream"),
            )
        )
        if document is None:
            failed.append(file.name)
        else:
            succeeded.append(document)

    progress.progress(1.0, text="Done")
    progress.empty()

    for document in succeeded:
        # With INGESTION_MODE=inline the upload response already carries the
        # real outcome, so a failure is reported here rather than being
        # discovered later in the list.
        if document["status"] == "failed":
            st.error(
                f"**{document['filename']}** could not be processed: "
                f"{document.get('error_message') or 'unknown error'}",
                icon=":material/error:",
            )
        elif document["status"] == "ready":
            st.success(
                f"**{document['filename']}** is ready — {document['chunk_count']} chunks.",
                icon=":material/check:",
            )
        else:
            st.info(
                f"**{document['filename']}** is {document['status']}; "
                "it will become searchable shortly.",
                icon=":material/schedule:",
            )
    if failed:
        st.warning(f"Not uploaded: {', '.join(failed)}")
    if succeeded:
        st.rerun()


def _document_list(namespace: dict[str, Any], collection: dict[str, Any]) -> None:
    page = guard(
        lambda: client().list_documents(namespace["id"], collection["id"]),
        spinner="Loading documents…",
    )
    documents = items(page)
    if not documents:
        empty_state("No documents in this collection yet.")
        return

    waiting = [d for d in documents if d["status"] in IN_FLIGHT]
    header, refresh = st.columns([4, 1])
    header.subheader(f"{page['total']} document(s)")
    if waiting:
        header.caption(f"{len(waiting)} still being processed.")
    if refresh.button("Refresh", icon=":material/refresh:", use_container_width=True):
        st.rerun()

    for document in documents:
        _document_row(namespace, document)


def _document_row(namespace: dict[str, Any], document: dict[str, Any]) -> None:
    status = document["status"]
    with st.container(border=True):
        title, state, actions = st.columns([4, 2, 1])
        title.markdown(f"**{document.get('title') or document['filename']}**")
        # The MIME type is dropped: it restates the file extension, and
        # `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
        # tells a reader nothing the filename did not.
        title.caption(
            f"{document['size_bytes'] / 1024:.0f} KB · uploaded {when(document.get('created_at'))}"
        )
        state.markdown(status_badge(status), help=status_help(status))
        if status == "ready":
            state.caption(
                f"{document['chunk_count']} chunks"
                + (f" · {document['page_count']} pages" if document.get("page_count") else "")
            )

        if status == "failed" and document.get("error_message"):
            title.error(document["error_message"], icon=":material/report:")
        elif status == "queued" and document.get("error_message"):
            # Queued *with* an error message means an attempt failed and
            # another is already scheduled. Saying so stops it reading as a
            # document that is simply slow.
            title.warning(
                f"Retrying after: {document['error_message']}", icon=":material/autorenew:"
            )

        with actions:
            if st.button(
                "Reprocess",
                key=f"reprocess-{document['id']}",
                use_container_width=True,
                help="Run extraction and embedding again. Replaces the existing chunks "
                "rather than adding to them, so it is safe to repeat.",
            ):
                result = guard(
                    lambda: client().reprocess_document(namespace["id"], document["id"]),
                    spinner="Reprocessing…",
                )
                if result is not None:
                    st.rerun()
            remove = st.button(
                "Delete",
                key=f"delete-{document['id']}",
                use_container_width=True,
                help="Removes the document immediately. Its vectors are purged by a "
                "background job, which retries if the index is unavailable.",
            )
            if remove and guard(lambda: client().delete_document(namespace["id"], document["id"])):
                st.rerun()

        with st.expander("Processing history"):
            _jobs(namespace, document)


def _jobs(namespace: dict[str, Any], document: dict[str, Any]) -> None:
    jobs = guard(lambda: client().list_document_jobs(namespace["id"], document["id"]))
    if not jobs:
        st.caption("No jobs recorded for this document.")
        return

    for job in jobs:
        icon = JOB_STATUS_STYLE.get(job["status"], "•")
        line = (
            f"{icon} **{job['kind']}** — {job['status']} · "
            f"attempt {job['attempts']} of {job['max_attempts']} · "
            f"{when(job.get('finished_at') or job.get('started_at') or job['created_at'])}"
        )
        st.markdown(line)
        if job.get("last_error"):
            st.caption(f"↳ {job['last_error']}")
