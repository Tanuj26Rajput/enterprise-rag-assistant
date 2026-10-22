"""Workspaces (namespaces), their members, and the collections inside them.

The two levels of the tenancy model are on one page because they are read
together: "which workspace am I in, who else is in it, and what collections
does it hold" is a single question in practice.

Role-sensitive controls are hidden when the caller's role cannot use them -
but only as a courtesy. Every one of these actions is authorized server-side,
and hiding a button is a UI decision, never a security boundary. A user who
posts the request by hand still gets a 403.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.components import empty_state, when
from frontend.state import client, guard, items, require_namespace, select_namespace

ROLES = ["owner", "admin", "member", "viewer"]
#: Roles that may create collections or upload. Mirrors `can_write()` on the
#: backend enum; the backend is what enforces it.
WRITER_ROLES = {"owner", "admin", "member"}


def render() -> None:
    st.title("Workspaces")

    namespaces = items(guard(client().list_namespaces, spinner="Loading workspaces…"))
    _create_workspace(namespaces)

    if not namespaces:
        empty_state(
            "You are not a member of any workspace yet.",
            "Create one above, or ask a colleague to invite you to theirs.",
        )
        return

    namespace = require_namespace()
    if namespace is None:
        return

    # The sidebar copy may be stale by a click; re-read the authoritative row.
    current = next((n for n in namespaces if n["id"] == namespace["id"]), namespace)
    role = current.get("role", "viewer")

    st.subheader(current["name"])
    if current.get("description"):
        st.write(current["description"])
    left, middle, right = st.columns(3)
    left.metric("Your role", role)
    middle.metric("Slug", current["slug"])
    right.metric("Created", when(current.get("created_at")))

    members_tab, collections_tab = st.tabs(["Members", "Collections"])
    with members_tab:
        _members(current, role)
    with collections_tab:
        _collections(current, role)


def _create_workspace(existing: list[dict[str, Any]]) -> None:
    with st.expander("New workspace", expanded=not existing):
        with st.form("create-namespace", clear_on_submit=True):
            name = st.text_input("Name", placeholder="Acme Legal")
            slug = st.text_input(
                "Slug",
                placeholder="acme-legal",
                help="Lowercase letters, digits and hyphens. Permanent: it appears in URLs "
                "and in the name of the underlying vector collection.",
            )
            description = st.text_area("Description (optional)")
            submitted = st.form_submit_button("Create workspace", type="primary")

        if not submitted:
            return
        if not name or not slug:
            st.error("A name and a slug are both required.")
            return

        created = guard(
            lambda: client().create_namespace(
                slug=slug.strip().lower(), name=name.strip(), description=description
            ),
            spinner="Creating…",
        )
        if created is not None:
            select_namespace({**created, "role": "owner"})
            st.success(f"Created {created['name']}.")
            st.rerun()


def _members(namespace: dict[str, Any], role: str) -> None:
    members = items(guard(lambda: client().list_members(namespace["id"])))
    if members is None:
        return

    if role in {"owner", "admin"}:
        with st.form("invite", clear_on_submit=True):
            columns = st.columns([3, 2, 1])
            email = columns[0].text_input("Email", label_visibility="collapsed")
            # Admins cannot grant ownership - that would be an escalation past
            # their own authority, and the API refuses it.
            grantable = ROLES if role == "owner" else ROLES[1:]
            new_role = columns[1].selectbox(
                "Role", grantable, index=len(grantable) - 2, label_visibility="collapsed"
            )
            if columns[2].form_submit_button("Invite", use_container_width=True) and email:
                added = guard(
                    lambda: client().add_member(
                        namespace["id"], email=email.strip().lower(), role=new_role
                    )
                )
                if added is not None:
                    st.success(f"Added {email} as {new_role}.")
                    st.rerun()

    if not members:
        empty_state("No members yet.")
        return

    st.dataframe(
        [
            {
                "Role": member["role"],
                "Member since": when(member.get("created_at")),
                "User ID": member["user_id"],
            }
            for member in members
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Email addresses are shown only to the person they belong to; the API returns "
        "membership by user id."
    )


def _collections(namespace: dict[str, Any], role: str) -> None:
    collections = items(guard(lambda: client().list_collections(namespace["id"])))

    if role in WRITER_ROLES:
        with st.expander("New collection", expanded=not collections):
            with st.form("create-collection", clear_on_submit=True):
                name = st.text_input("Name", placeholder="Employee handbook")
                slug = st.text_input("Slug", placeholder="handbook")
                description = st.text_area("Description (optional)")
                submitted = st.form_submit_button("Create collection", type="primary")

            if submitted and name and slug:
                created = guard(
                    lambda: client().create_collection(
                        namespace["id"],
                        slug=slug.strip().lower(),
                        name=name.strip(),
                        description=description,
                    ),
                    spinner="Creating…",
                )
                if created is not None:
                    st.success(f"Created {created['name']}.")
                    st.rerun()
            elif submitted:
                st.error("A name and a slug are both required.")

    if not collections:
        empty_state(
            "No collections in this workspace yet.",
            "A collection groups documents that are searched together.",
        )
        return

    for collection in collections:
        with st.container(border=True):
            header, actions = st.columns([4, 1])
            header.markdown(f"**{collection['name']}** · `{collection['slug']}`")
            header.caption(
                f"{collection['document_count']} documents · "
                f"{collection['chunk_count']} chunks · "
                f"embedded with `{collection['embedding_model']}` "
                f"({collection['embedding_dimensions']} dimensions)"
            )
            if collection.get("description"):
                header.write(collection["description"])

            sweep = role in {"owner", "admin"} and actions.button(
                "Reconcile",
                key=f"reconcile-{collection['id']}",
                use_container_width=True,
                help="Remove vectors with no live chunk behind them. Safe to run any "
                "time; it never touches data a document still owns.",
            )
            if sweep:
                # `collection` is bound as a default rather than captured:
                # the lambda outlives this iteration in a closure, and the
                # late-binding bug it would otherwise have is silent.
                result = guard(
                    lambda collection=collection: client().reconcile_collection(
                        namespace["id"], collection["id"]
                    ),
                    spinner="Sweeping the index…",
                )
                if result is not None:
                    removed = result["vectors_removed"]
                    st.success(
                        f"Removed {removed} orphaned vector(s)."
                        if removed
                        else "Nothing to clean up — the index matches the database."
                    )
