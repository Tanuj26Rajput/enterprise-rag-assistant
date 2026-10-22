"""Streaming chat with citations, and the conversation history beside it.

The one genuinely interesting piece here is the streaming loop. Streamlit
re-runs the script on every interaction, so a naive implementation would ask
the question again on each re-run. The pattern used instead: a submitted
question is parked in session state, the script re-runs, and the re-run is
what consumes it - exactly once, because consuming it clears it.

Tokens are rendered as they arrive and the citations only at the end. That is
not a shortcut: a bracketed marker is resolvable only once the text
containing it is complete, so the backend cannot send citations earlier
without guessing, and guessing is how citations get invented.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.components import answer_quality, empty_state, render_citations, when
from frontend.state import client, guard, items, require_namespace


def render() -> None:
    namespace = require_namespace()
    if namespace is None:
        return

    collections = items(guard(lambda: client().list_collections(namespace["id"])))
    if not collections:
        st.title("Chat")
        empty_state(
            "This workspace has no collections to search.",
            "Create one and upload a document first.",
        )
        return

    conversation = _sidebar(namespace, collections)
    if conversation is None:
        st.title("Chat")
        empty_state(
            "Start a conversation from the sidebar.",
            "Each thread is tied to one collection and is private to you.",
        )
        return

    _thread(namespace, conversation)


# ------------------------------------------------------------------ sidebar ---
def _sidebar(namespace: dict[str, Any], collections: list[dict[str, Any]]) -> dict[str, Any] | None:
    with st.sidebar:
        st.subheader("Conversations")

        with st.popover("New conversation", use_container_width=True):
            collection = st.selectbox(
                "Collection",
                collections,
                format_func=lambda c: c["name"],
                key="new-conversation-collection",
            )
            title = st.text_input("Title (optional)", key="new-conversation-title")
            if st.button("Start", type="primary", use_container_width=True):
                created = guard(
                    lambda: client().create_conversation(
                        namespace["id"],
                        collection_id=collection["id"],
                        title=title.strip() or None,
                    )
                )
                if created is not None:
                    st.session_state.conversation_id = created["id"]
                    st.rerun()

        conversations = items(guard(lambda: client().list_conversations(namespace["id"])))
        if not conversations:
            st.caption("No conversations yet.")
            return None

        current_id = st.session_state.get("conversation_id")
        if current_id not in {c["id"] for c in conversations}:
            current_id = conversations[0]["id"]
            st.session_state.conversation_id = current_id

        for conversation in conversations:
            selected = conversation["id"] == current_id
            label = conversation.get("title") or "Untitled"
            if st.button(
                f"{'▸ ' if selected else ''}{label}",
                key=f"conversation-{conversation['id']}",
                use_container_width=True,
                type="primary" if selected else "secondary",
                help=f"{conversation['message_count']} messages · "
                f"{when(conversation.get('last_message_at') or conversation['created_at'])}",
            ):
                st.session_state.conversation_id = conversation["id"]
                st.rerun()

        return next(c for c in conversations if c["id"] == current_id)


# ------------------------------------------------------------------- thread ---
def _thread(namespace: dict[str, Any], conversation: dict[str, Any]) -> None:
    header, actions = st.columns([4, 1])
    header.title(conversation.get("title") or "Untitled conversation")
    # Deliberately not the model id or the token count: which model answered
    # and what it cost are operator facts, available on the API and in the
    # logs. A reader of the conversation needs neither to judge the answer,
    # and naming the model invites trusting it over the citations.
    header.caption(f"{conversation['message_count']} messages")
    with actions.popover("Manage", use_container_width=True):
        new_title = st.text_input("Rename", value=conversation.get("title") or "")
        rename = st.button("Save", use_container_width=True)
        remove = st.button("Delete conversation", use_container_width=True)

    if rename and new_title.strip():
        renamed = guard(
            lambda: client().rename_conversation(
                namespace["id"], conversation["id"], title=new_title.strip()
            )
        )
        if renamed is not None:
            st.rerun()
    if remove and guard(lambda: client().delete_conversation(namespace["id"], conversation["id"])):
        st.session_state.pop("conversation_id", None)
        st.rerun()

    messages = items(guard(lambda: client().list_messages(namespace["id"], conversation["id"])))
    for message in messages:
        _render_message(message)

    pending = st.session_state.pop("pending_question", None)
    if pending:
        # Echo the question immediately: it is already durable on the server,
        # and the user should see what they asked while the answer builds.
        with st.chat_message("user"):
            st.markdown(pending)
        _stream(namespace, conversation, pending)
        st.rerun()

    question = st.chat_input("Ask a question about these documents…")
    if question:
        # Parked rather than answered here, so the next re-run owns the
        # streaming. Answering inline would re-ask on every widget event.
        st.session_state.pending_question = question
        st.rerun()


def _render_message(message: dict[str, Any]) -> None:
    role = "user" if message["role"] == "user" else "assistant"
    with st.chat_message(role):
        if message.get("error"):
            st.error(message["error"], icon=":material/error:")
        st.markdown(message["content"] or "_(no answer)_")
        if role == "assistant":
            render_citations(message.get("citations") or [], key=message["id"])


def _stream(namespace: dict[str, Any], conversation: dict[str, Any], question: str) -> None:
    """Render one streamed answer.

    Both failure modes are handled in-band, because by the time an answer is
    streaming the HTTP status is already 200: an `error` event is the only
    way the backend can report a provider outage, and a dropped connection
    arrives as a transport exception the client turns into the same event.
    Either way the user sees a sentence, and the question remains saved
    server-side so retrying costs nothing.
    """
    api = client()
    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.caption("Searching the collection…")
        text_parts: list[str] = []
        done: dict[str, Any] | None = None
        failure: dict[str, Any] | None = None

        for item in api.stream_answer(namespace["id"], conversation["id"], question=question):
            if item.name == "start":
                retrieved = item.data.get("retrieved_count", 0)
                placeholder.caption(
                    f"Found {retrieved} relevant passage(s); composing an answer…"
                    if retrieved
                    else "No matching passages found…"
                )
            elif item.name == "token":
                text_parts.append(item.data.get("delta", ""))
                # The cursor makes a slow stream read as progress rather than
                # as a stall.
                placeholder.markdown("".join(text_parts) + "▌")
            elif item.name == "done":
                done = item.data
            elif item.name == "error":
                failure = item.data

        answer = "".join(text_parts)
        if answer:
            placeholder.markdown(answer)
        elif failure is None:
            placeholder.markdown("_(no answer)_")
        else:
            placeholder.empty()

        if failure is not None:
            st.error(
                failure.get("message") or "The answer could not be completed.",
                icon=":material/error:",
            )
            if answer:
                st.caption("The partial answer above was saved to this conversation.")
            return

        if done is not None:
            answer_quality(done)
            render_citations(done.get("citations") or [], key="live")
