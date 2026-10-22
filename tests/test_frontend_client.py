"""The frontend's API client, which is where its only real logic lives.

The views are Streamlit layout and are not worth asserting against - a test
that checks `st.button` was called teaches nothing. The client is different:
it decodes a wire protocol, handles token refresh, and translates failures
into something a user can read. All three are easy to get subtly wrong and
all three are testable without a browser.

The transport is stubbed rather than reached: these are tests of the decoding
and the retry logic, not of `requests`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests

from frontend.api import ApiClient, ApiError, StreamEvent, Tokens, _decode_sse
from frontend.config import FORBIDDEN_ENV_VARS, FrontendSettings, leaked_secrets, load_settings

SETTINGS = FrontendSettings(api_url="http://api.test")


class StubResponse:
    """Enough of `requests.Response` for the client to work against."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        lines: list[str] | None = None,
        raise_on_iter: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.reason = "OK" if status_code < 400 else "Error"
        self._payload = payload
        self._body = body
        self.headers = headers or {}
        self._lines = lines or []
        self._raise_on_iter = raise_on_iter
        self.closed = False

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def content(self) -> bytes:
        if self._body is not None:
            return self._body.encode()
        return b"" if self._payload is None else json.dumps(self._payload).encode()

    def json(self) -> Any:
        if self._body is not None:
            raise ValueError("not json")
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    def iter_lines(self, decode_unicode: bool = False) -> Any:
        if self._raise_on_iter is not None:
            raise self._raise_on_iter
        yield from self._lines

    def __enter__(self) -> StubResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


class StubSession:
    """Records calls and replays scripted responses."""

    def __init__(self, *responses: StubResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected call: {method} {url}")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        return self._next(method, url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> StubResponse:
        return self._next("POST", url, **kwargs)

    def close(self) -> None:
        return None


def make_client(*responses: StubResponse | Exception) -> tuple[ApiClient, StubSession]:
    client = ApiClient(SETTINGS)
    session = StubSession(*responses)
    client._http = session  # type: ignore[assignment]
    return client, session


# ------------------------------------------------------------ configuration ---
class TestConfiguration:
    def test_the_frontend_knows_only_an_api_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole security argument for this process in one assertion.

        If `FrontendSettings` ever grows a credential field, this fails -
        which is the point. A frontend that holds no secret cannot leak one.
        """
        fields = set(FrontendSettings.__dataclass_fields__)
        assert fields == {"api_url", "request_timeout", "stream_timeout", "upload_timeout"}

    def test_the_api_root_is_versioned(self) -> None:
        assert SETTINGS.api_root == "http://api.test/api/v1"

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        assert FrontendSettings(api_url="http://api.test/").api_root == "http://api.test/api/v1"

    def test_it_falls_back_to_localhost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RAG_API_URL", raising=False)
        assert load_settings().api_url == "http://127.0.0.1:8000"

    def test_a_blank_url_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A `.env` template leaves keys blank; blank must not mean "connect to ''"."""
        monkeypatch.setenv("RAG_API_URL", "   ")
        assert load_settings().api_url == "http://127.0.0.1:8000"

    def test_backend_secrets_in_the_environment_are_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in FORBIDDEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-not-be-here")
        monkeypatch.setenv("DATABASE_URL", "postgresql://...")

        assert leaked_secrets() == ["DATABASE_URL", "OPENROUTER_API_KEY"]

    def test_a_clean_environment_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in FORBIDDEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        assert leaked_secrets() == []


# ---------------------------------------------------------------- transport ---
class TestErrorTranslation:
    def test_the_api_error_envelope_is_decoded(self) -> None:
        client, _ = make_client(
            StubResponse(
                403,
                {
                    "error": {
                        "code": "permission_denied",
                        "message": "You need the admin role.",
                        "request_id": "abc123",
                    }
                },
            )
        )
        client.tokens = Tokens("access", "refresh")

        with pytest.raises(ApiError) as caught:
            client.list_namespaces()

        assert caught.value.code == "permission_denied"
        assert caught.value.message == "You need the admin role."
        assert caught.value.request_id == "abc123"
        assert caught.value.is_auth_failure

    def test_a_non_json_failure_still_produces_a_sentence(self) -> None:
        """A proxy's HTML 502 page must not surface as a JSON decode error."""
        client, _ = make_client(StubResponse(502, body="<html>Bad Gateway</html>"))
        client.tokens = Tokens("access", "refresh")

        with pytest.raises(ApiError) as caught:
            client.list_namespaces()

        assert caught.value.code == "unexpected_response"
        assert "502" in caught.value.message

    def test_a_json_body_that_is_not_our_envelope_is_handled(self) -> None:
        client, _ = make_client(StubResponse(500, {"detail": "something else"}))
        client.tokens = Tokens("access", "refresh")

        with pytest.raises(ApiError) as caught:
            client.list_namespaces()

        assert caught.value.code == "unexpected_response"

    def test_an_unreachable_api_says_so_plainly(self) -> None:
        client, _ = make_client(requests.ConnectionError("refused"))

        with pytest.raises(ApiError) as caught:
            client.list_namespaces()

        assert caught.value.code == "unreachable"
        assert "http://api.test" in caught.value.message

    def test_a_timeout_is_distinguished_from_a_refusal(self) -> None:
        client, _ = make_client(requests.Timeout("too slow"))

        with pytest.raises(ApiError) as caught:
            client.list_namespaces()

        assert caught.value.code == "timeout"

    def test_a_204_returns_nothing_rather_than_failing_to_parse(self) -> None:
        client, _ = make_client(StubResponse(204))
        client.tokens = Tokens("access", "refresh")

        assert client.delete_document("n", "d") is True


class TestAuthentication:
    def test_the_token_is_attached_to_every_call(self) -> None:
        client, session = make_client(StubResponse(200, {"items": []}))
        client.tokens = Tokens("the-access-token", "refresh")

        client.list_namespaces()

        assert session.calls[0]["headers"]["Authorization"] == "Bearer the-access-token"

    def test_no_authorization_header_before_sign_in(self) -> None:
        client, session = make_client(StubResponse(200, {}))

        client._request("GET", "/auth/me")

        assert "Authorization" not in session.calls[0]["headers"]

    def test_login_stores_the_pair_and_the_expiry(self) -> None:
        expires = datetime.now(UTC) + timedelta(minutes=30)
        client, _ = make_client(
            StubResponse(
                200,
                {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_at": expires.isoformat(),
                },
            )
        )

        tokens = client.login("user@example.com", "password")

        assert tokens.access_token == "a"
        assert tokens.expires_at is not None
        assert not tokens.is_due_for_refresh

    def test_a_401_triggers_one_refresh_and_one_retry(self) -> None:
        """An access token expiring mid-session must be invisible to the user."""
        client, session = make_client(
            StubResponse(401, {"error": {"code": "authentication_failed", "message": "expired"}}),
            StubResponse(200, {"access_token": "new", "refresh_token": "r2"}),
            StubResponse(200, {"items": [{"id": "n1"}]}),
        )
        client.tokens = Tokens("stale", "refresh")

        result = client.list_namespaces()

        assert result["items"] == [{"id": "n1"}]
        assert client.tokens is not None and client.tokens.access_token == "new"
        assert session.calls[-1]["headers"]["Authorization"] == "Bearer new"

    def test_the_retry_happens_only_once(self) -> None:
        """Otherwise a genuinely revoked token loops against the API."""
        client, session = make_client(
            StubResponse(401, {"error": {"code": "authentication_failed", "message": "no"}}),
            StubResponse(200, {"access_token": "new", "refresh_token": "r2"}),
            StubResponse(401, {"error": {"code": "authentication_failed", "message": "no"}}),
        )
        client.tokens = Tokens("stale", "refresh")

        with pytest.raises(ApiError):
            client.list_namespaces()

        assert len(session.calls) == 3

    def test_a_rejected_refresh_clears_the_session(self) -> None:
        """Signed out, rather than left holding a token that cannot work."""
        client, _ = make_client(
            StubResponse(401, {"error": {"code": "authentication_failed", "message": "no"}}),
            StubResponse(401, {"error": {"code": "authentication_failed", "message": "no"}}),
        )
        client.tokens = Tokens("stale", "refresh")

        with pytest.raises(ApiError):
            client.list_namespaces()

        assert client.tokens is None
        assert not client.is_signed_in

    def test_a_refresh_that_cannot_reach_the_api_does_not_raise(self) -> None:
        """A network blip during refresh is not the user's problem to see."""
        client, _ = make_client(requests.ConnectionError("down"))
        client.tokens = Tokens("stale", "refresh")

        assert client._try_refresh() is False

    def test_a_token_near_expiry_is_refreshed_before_the_call(self) -> None:
        client, session = make_client(
            StubResponse(200, {"access_token": "fresh", "refresh_token": "r2"}),
            StubResponse(200, {"items": []}),
        )
        client.tokens = Tokens(
            "stale", "refresh", expires_at=datetime.now(UTC) + timedelta(seconds=5)
        )

        client.list_namespaces()

        assert session.calls[0]["url"].endswith("/auth/refresh")
        assert session.calls[1]["headers"]["Authorization"] == "Bearer fresh"

    def test_signing_out_discards_the_token(self) -> None:
        client, _ = make_client()
        client.tokens = Tokens("a", "r")

        client.sign_out()

        assert client.tokens is None

    def test_an_unparseable_expiry_does_not_break_the_session(self) -> None:
        """A server sending something unexpected should not log the user out."""
        client, _ = make_client(
            StubResponse(200, {"access_token": "a", "refresh_token": "r", "expires_at": "soon"})
        )

        tokens = client.login("user@example.com", "password")

        assert tokens.expires_at is None
        assert not tokens.is_due_for_refresh


# --------------------------------------------------------------------- SSE ---
class TestSseDecoding:
    def test_events_are_reassembled_at_the_blank_line(self) -> None:
        lines = [
            "event: start",
            'data: {"conversation_id":"c1","retrieved_count":3}',
            "",
            "event: token",
            'data: {"delta":"Employees "}',
            "",
            "event: token",
            'data: {"delta":"accrue 25 days."}',
            "",
        ]

        events = list(_decode_sse(iter(lines)))

        assert [e.name for e in events] == ["start", "token", "token"]
        assert events[0].data["retrieved_count"] == 3
        assert "".join(e.data["delta"] for e in events[1:]) == "Employees accrue 25 days."

    def test_keep_alive_comments_are_ignored(self) -> None:
        """A proxy-defeating comment must not surface as an empty event."""
        lines = [": keep-alive", "", "event: token", 'data: {"delta":"hi"}', ""]

        events = list(_decode_sse(iter(lines)))

        assert [e.name for e in events] == ["token"]

    def test_a_final_event_without_a_trailing_blank_line_is_not_lost(self) -> None:
        lines = ["event: done", 'data: {"answer":"yes"}']

        events = list(_decode_sse(iter(lines)))

        assert events[0].name == "done"
        assert events[0].data["answer"] == "yes"

    def test_multi_line_data_is_joined(self) -> None:
        lines = ["event: done", 'data: {"answer":', 'data: "split"}', ""]

        events = list(_decode_sse(iter(lines)))

        assert events[0].data["answer"] == "split"

    def test_an_unnamed_event_defaults_to_message(self) -> None:
        events = list(_decode_sse(iter(['data: {"a":1}', ""])))

        assert events[0].name == "message"

    def test_data_that_is_not_json_is_preserved_rather_than_dropped(self) -> None:
        events = list(_decode_sse(iter(["event: token", "data: not json", ""])))

        assert events[0].data == {"raw": "not json"}

    def test_the_leading_space_after_the_colon_is_stripped_once(self) -> None:
        """`data:  x` carries a space; `data: x` does not. Getting this wrong
        inserts a space into every token of every answer."""
        events = list(_decode_sse(iter(["event: token", "data:  leading", ""])))

        assert events[0].data == {"raw": " leading"}


class TestStreaming:
    def _client_streaming(self, lines: list[str]) -> ApiClient:
        client, _ = make_client(StubResponse(200, lines=lines))
        client.tokens = Tokens("a", "r")
        return client

    def test_a_whole_turn_arrives_in_order(self) -> None:
        client = self._client_streaming(
            [
                "event: start",
                'data: {"retrieved_count":2}',
                "",
                "event: token",
                'data: {"delta":"Twenty-five days."}',
                "",
                "event: done",
                'data: {"answer":"Twenty-five days.","citations":[{"marker":1}]}',
                "",
            ]
        )

        events = list(client.stream_answer("n", "c", question="How much leave?"))

        assert [e.name for e in events] == ["start", "token", "done"]
        assert events[-1].data["citations"] == [{"marker": 1}]

    def test_the_accept_header_asks_for_an_event_stream(self) -> None:
        client, session = make_client(StubResponse(200, lines=[]))
        client.tokens = Tokens("a", "r")

        list(client.stream_answer("n", "c", question="hi"))

        assert session.calls[0]["headers"]["Accept"] == "text/event-stream"
        assert session.calls[0]["stream"] is True

    def test_a_failure_before_the_stream_starts_becomes_an_error_event(self) -> None:
        """The caller has one code path for "it did not work", not two."""
        client, _ = make_client(
            StubResponse(
                404, {"error": {"code": "not_found", "message": "Conversation not found."}}
            )
        )
        client.tokens = Tokens("a", "r")

        events = list(client.stream_answer("n", "missing", question="hi"))

        assert len(events) == 1
        assert events[0].name == "error"
        assert events[0].data["code"] == "not_found"

    def test_a_connection_dropped_mid_stream_is_reported_in_band(self) -> None:
        client, _ = make_client(
            StubResponse(200, raise_on_iter=requests.ConnectionError("reset by peer"))
        )
        client.tokens = Tokens("a", "r")

        events = list(client.stream_answer("n", "c", question="hi"))

        assert events[-1].name == "error"
        assert events[-1].data["code"] == "stream_interrupted"

    def test_an_unreachable_api_does_not_raise_out_of_the_generator(self) -> None:
        client, _ = make_client(requests.ConnectionError("refused"))
        client.tokens = Tokens("a", "r")

        events = list(client.stream_answer("n", "c", question="hi"))

        assert events[0].name == "error"
        assert events[0].data["code"] == "unreachable"

    def test_the_response_is_closed_even_on_a_partial_read(self) -> None:
        """Otherwise an abandoned stream holds a connection open indefinitely."""
        response = StubResponse(200, lines=["event: token", 'data: {"delta":"a"}', ""])
        client, _ = make_client(response)
        client.tokens = Tokens("a", "r")

        list(client.stream_answer("n", "c", question="hi"))

        assert response.closed

    def test_an_optional_collection_override_is_sent_only_when_given(self) -> None:
        client, session = make_client(StubResponse(200, lines=[]), StubResponse(200, lines=[]))
        client.tokens = Tokens("a", "r")

        list(client.stream_answer("n", "c", question="hi"))
        list(client.stream_answer("n", "c", question="hi", collection_id="col-1"))

        assert "collection_id" not in session.calls[0]["json"]
        assert session.calls[1]["json"]["collection_id"] == "col-1"


class TestRequestShapes:
    """The client must speak the schemas the API actually declares."""

    def test_upload_sends_a_multipart_file(self) -> None:
        client, session = make_client(StubResponse(201, {"id": "d1"}))
        client.tokens = Tokens("a", "r")

        client.upload_document(
            "n",
            "c",
            filename="handbook.pdf",
            content=b"%PDF-",
            content_type="application/pdf",
            title="Handbook",
        )

        call = session.calls[0]
        assert call["files"]["file"] == ("handbook.pdf", b"%PDF-", "application/pdf")
        assert call["data"] == {"title": "Handbook"}

    def test_upload_omits_the_title_when_there_is_none(self) -> None:
        client, session = make_client(StubResponse(201, {"id": "d1"}))
        client.tokens = Tokens("a", "r")

        client.upload_document("n", "c", filename="a.txt", content=b"x", content_type="text/plain")

        assert session.calls[0]["data"] is None

    def test_upload_gets_a_longer_timeout_than_a_read(self) -> None:
        """Inline ingestion means the upload call covers extraction too."""
        client, session = make_client(StubResponse(201, {}), StubResponse(200, {"items": []}))
        client.tokens = Tokens("a", "r")

        client.upload_document("n", "c", filename="a.txt", content=b"x", content_type="text/plain")
        client.list_namespaces()

        assert session.calls[0]["timeout"] > session.calls[1]["timeout"]

    def test_conversation_creation_sends_the_collection(self) -> None:
        client, session = make_client(StubResponse(201, {"id": "c1"}))
        client.tokens = Tokens("a", "r")

        client.create_conversation("n", collection_id="col-1", title="Leave policy")

        assert session.calls[0]["json"] == {
            "collection_id": "col-1",
            "title": "Leave policy",
        }

    def test_search_omits_top_k_when_unset(self) -> None:
        client, session = make_client(StubResponse(200, {"hits": []}))
        client.tokens = Tokens("a", "r")

        client.search("n", "c", query="leave")

        assert session.calls[0]["json"] == {"query": "leave"}


def test_stream_event_defaults_to_an_empty_payload() -> None:
    assert StreamEvent("ping").data == {}
