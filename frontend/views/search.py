"""Retrieval without generation.

Worth having as its own page: it shows what the model would have been given,
which is the fastest way to tell "the model answered badly" apart from "the
right passage was never retrieved". Those two problems have completely
different fixes, and without this page they look identical from the chat.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.components import empty_state
from frontend.state import client, guard, items, require_namespace


def render() -> None:
    st.title("Search")
    st.caption("Find the passages themselves, without asking a question about them.")

    namespace = require_namespace()
    if namespace is None:
        return

    collections = items(guard(lambda: client().list_collections(namespace["id"])))
    if not collections:
        empty_state("This workspace has no collections to search.")
        return

    collection = st.selectbox(
        "Collection", collections, format_func=lambda c: c["name"], key="search-collection"
    )
    query = st.text_input("Query", placeholder="How much annual leave do employees get?")
    top_k = st.slider("Passages to return", min_value=1, max_value=20, value=5)

    if not st.button("Search", type="primary", icon=":material/search:") or not query.strip():
        return

    response = guard(
        lambda: client().search(
            namespace["id"], collection["id"], query=query.strip(), top_k=top_k
        ),
        spinner="Searching…",
    )
    if response is None:
        return

    hits = response.get("hits") or []
    if not hits:
        empty_state(
            "Nothing matched.",
            "Either the collection has no relevant text, or nothing has finished processing yet.",
        )
        return

    st.caption(f"{len(hits)} passage(s), most similar first.")
    for hit in hits:
        _hit(hit)


def _hit(hit: dict[str, Any]) -> None:
    with st.container(border=True):
        name = hit.get("document_title") or "Untitled document"
        page = hit.get("page_number")
        score = hit.get("score")
        heading = f"**{name}**"
        if page:
            heading += f" · page {page}"
        if isinstance(score, int | float):
            heading += f" · score {score:.3f}"
        st.markdown(heading)
        st.markdown((hit.get("content") or "").strip())
