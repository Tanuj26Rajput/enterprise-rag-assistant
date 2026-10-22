"""Sign in and registration.

The password never leaves this function except in the body of a POST to our
own API over the configured URL. It is not stored in session state, not
logged, and not echoed back - Streamlit's `type="password"` input keeps it
out of the rendered DOM too.
"""

from __future__ import annotations

import streamlit as st

from frontend.api import ApiError
from frontend.state import client, guard, render_error

MIN_PASSWORD_LENGTH = 12


def render() -> None:
    st.title("Enterprise RAG Assistant")
    st.caption("Ask questions across your documents, with citations you can check.")

    sign_in, register = st.tabs(["Sign in", "Create an account"])
    with sign_in:
        _sign_in_form()
    with register:
        _register_form()


def _sign_in_form() -> None:
    with st.form("sign-in", clear_on_submit=False):
        email = st.text_input("Email", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if not submitted:
        return
    if not email or not password:
        st.error("Enter your email and password.")
        return

    api = client()
    try:
        with st.spinner("Signing in…"):
            api.login(email.strip(), password)
            user = api.me()
    except ApiError as exc:
        # The API answers an unknown email and a wrong password identically,
        # and so does this: distinguishing them would turn the sign-in form
        # into a way to find out who has an account here.
        render_error(exc)
        return

    st.session_state.user = user
    st.rerun()


def _register_form() -> None:
    with st.form("register", clear_on_submit=False):
        email = st.text_input("Email", key="register-email", autocomplete="username")
        full_name = st.text_input("Full name (optional)", key="register-name")
        password = st.text_input(
            "Password",
            type="password",
            key="register-password",
            autocomplete="new-password",
            help=f"At least {MIN_PASSWORD_LENGTH} characters. Length matters more than symbols.",
        )
        confirm = st.text_input("Confirm password", type="password", key="register-confirm")
        submitted = st.form_submit_button("Create account", use_container_width=True)

    if not submitted:
        return

    # Checked here as well as server-side, purely so the user finds out
    # before a round trip. The API enforces both regardless.
    if not email or not password:
        st.error("Enter an email and a password.")
        return
    if password != confirm:
        st.error("The two passwords do not match.")
        return
    if len(password) < MIN_PASSWORD_LENGTH:
        st.error(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
        return

    api = client()
    created = guard(
        lambda: api.register(email.strip(), password, full_name.strip() or None),
        spinner="Creating your account…",
    )
    if created is None:
        return

    tokens = guard(lambda: api.login(email.strip(), password))
    if tokens is None:
        st.success("Account created. Sign in with your new credentials.")
        return

    user = guard(api.me)
    if user is not None:
        st.session_state.user = user
        st.rerun()
