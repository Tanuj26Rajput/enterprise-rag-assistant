"""The only code in the frontend that talks to anything.

One class, one responsibility: turn a UI action into an HTTP call against the
backend and turn the response back into either data or an :class:`ApiError`.
Nothing else in `frontend/` imports ``requests``, which is what keeps three
properties easy to verify:

* **No direct data access.** There is no database driver here and no model
  client. Every piece of information on screen came through a route that
  checked the caller's token and namespace role.
* **One place for auth.** The bearer header is attached here, refreshed here
  on a 401, and discarded here on sign-out. A view cannot forget it.
* **One error shape.** The API answers every failure with the same envelope;
  this decodes it once, so views render a message instead of a traceback.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import requests

from frontend.config import FrontendSettings

#: How long before expiry a token is considered due for refresh. Refreshing
#: slightly early is free; discovering expiry mid-request costs a round trip
#: and, on a stream, a confusing partial failure.
REFRESH_MARGIN_SECONDS = 60


class ApiError(Exception):
    """A failure the backend described, or one reaching it at all.

    ``code`` is the API's machine-readable code where there was a response to
    read it from, and a transport-level stand-in otherwise, so views can
    branch on it without parsing prose.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status: int | None = None,
        details: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details
        self.request_id = request_id

    @property
    def is_auth_failure(self) -> bool:
        return self.status in (401, 403)

    def __str__(self) -> str:
        return self.message


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: datetime | None = None

    @property
    def is_due_for_refresh(self) -> bool:
        if self.expires_at is None:
            return False
        remaining = (self.expires_at - datetime.now(UTC)).total_seconds()
        return remaining <= REFRESH_MARGIN_SECONDS


@dataclass
class StreamEvent:
    """One decoded server-sent event."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)


def _parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ApiClient:
    """A session-scoped client for one signed-in user.

    Held in Streamlit's per-session state, so two people using the same
    server never share a token: `st.session_state` is keyed by browser
    session, and this object never reads or writes a global.
    """

    def __init__(self, settings: FrontendSettings) -> None:
        self._settings = settings
        self._http = requests.Session()
        self.tokens: Tokens | None = None

    # ------------------------------------------------------------ plumbing ---
    def _url(self, path: str) -> str:
        return f"{self._settings.api_root}{path}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.tokens is not None:
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"
        return headers

    @staticmethod
    def _decode_error(response: requests.Response) -> ApiError:
        """Turn the API's error envelope into something renderable.

        Falls back to the status line when the body is not ours - a proxy's
        502 page, say - so an infrastructure failure still reaches the user
        as a sentence rather than as HTML in a toast.
        """
        request_id = response.headers.get("X-Request-ID")
        try:
            payload = response.json()
        except ValueError:
            return ApiError(
                f"The server returned {response.status_code} {response.reason}.",
                code="unexpected_response",
                status=response.status_code,
                request_id=request_id,
            )

        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return ApiError(
                f"The server returned {response.status_code} {response.reason}.",
                code="unexpected_response",
                status=response.status_code,
                request_id=request_id,
            )
        return ApiError(
            error.get("message") or "The request failed.",
            code=error.get("code", "error"),
            status=response.status_code,
            details=error.get("details"),
            request_id=error.get("request_id") or request_id,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        data: Any = None,
        timeout: float | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        self._refresh_if_due()
        try:
            response = self._http.request(
                method,
                self._url(path),
                json=json_body,
                params=params,
                files=files,
                data=data,
                headers=self._headers(),
                timeout=timeout or self._settings.request_timeout,
            )
        except requests.Timeout as exc:
            raise ApiError(
                "The API did not respond in time. It may be busy or still starting up.",
                code="timeout",
            ) from exc
        except requests.ConnectionError as exc:
            raise ApiError(
                f"Could not reach the API at {self._settings.api_url}. Is it running?",
                code="unreachable",
            ) from exc

        if response.status_code == 401 and retry_on_401 and self._try_refresh():
            return self._request(
                method,
                path,
                json_body=json_body,
                params=params,
                files=files,
                data=data,
                timeout=timeout,
                retry_on_401=False,
            )

        if not response.ok:
            raise self._decode_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ----------------------------------------------------------------- auth ---
    def _store(self, payload: dict[str, Any]) -> Tokens:
        self.tokens = Tokens(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            expires_at=_parse_expiry(payload.get("expires_at")),
        )
        return self.tokens

    def _refresh_if_due(self) -> None:
        if self.tokens is not None and self.tokens.is_due_for_refresh:
            self._try_refresh()

    def _try_refresh(self) -> bool:
        """Exchange the refresh token. False means the user must sign in again.

        Never raises: a failed refresh is an expected end of session, not an
        error to put in front of the user.
        """
        if self.tokens is None:
            return False
        try:
            response = self._http.post(
                self._url("/auth/refresh"),
                json={"refresh_token": self.tokens.refresh_token},
                timeout=self._settings.request_timeout,
            )
        except requests.RequestException:
            return False
        if not response.ok:
            self.tokens = None
            return False
        self._store(response.json())
        return True

    def register(self, email: str, password: str, full_name: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"email": email, "password": password}
        if full_name:
            body["full_name"] = full_name
        return self._request("POST", "/auth/register", json_body=body, retry_on_401=False)

    def login(self, email: str, password: str) -> Tokens:
        payload = self._request(
            "POST",
            "/auth/login",
            json_body={"email": email, "password": password},
            retry_on_401=False,
        )
        return self._store(payload)

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/auth/me")

    def sign_out(self) -> None:
        self.tokens = None
        self._http.close()
        self._http = requests.Session()

    @property
    def is_signed_in(self) -> bool:
        return self.tokens is not None

    # Deletes return True rather than None: the API answers 204 with no body,
    # and a caller needs to tell "it worked" from "it failed" without catching.

    # ----------------------------------------------------------- namespaces ---
    def list_namespaces(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self._request("GET", "/namespaces", params={"limit": limit, "offset": offset})

    def create_namespace(self, *, slug: str, name: str, description: str | None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/namespaces",
            json_body={"slug": slug, "name": name, "description": description or None},
        )

    def list_members(self, namespace_id: str) -> dict[str, Any]:
        return self._request("GET", f"/namespaces/{namespace_id}/members")

    def add_member(self, namespace_id: str, *, email: str, role: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/members",
            json_body={"email": email, "role": role},
        )

    def remove_member(self, namespace_id: str, member_id: str) -> bool:
        self._request("DELETE", f"/namespaces/{namespace_id}/members/{member_id}")
        return True

    # ---------------------------------------------------------- collections ---
    def list_collections(self, namespace_id: str, *, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "GET", f"/namespaces/{namespace_id}/collections", params={"limit": limit}
        )

    def create_collection(
        self, namespace_id: str, *, slug: str, name: str, description: str | None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/collections",
            json_body={"slug": slug, "name": name, "description": description or None},
        )

    def delete_collection(self, namespace_id: str, collection_id: str) -> bool:
        self._request("DELETE", f"/namespaces/{namespace_id}/collections/{collection_id}")
        return True

    def reconcile_collection(self, namespace_id: str, collection_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/collections/{collection_id}/reconcile",
            timeout=self._settings.upload_timeout,
        )

    # ------------------------------------------------------------ documents ---
    def upload_document(
        self,
        namespace_id: str,
        collection_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/collections/{collection_id}/documents",
            files={"file": (filename, content, content_type)},
            data={"title": title} if title else None,
            # Ingestion may run inside this request (INGESTION_MODE=inline),
            # so the timeout has to cover extraction and embedding, not just
            # the upload.
            timeout=self._settings.upload_timeout,
        )

    def list_documents(
        self, namespace_id: str, collection_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/namespaces/{namespace_id}/collections/{collection_id}/documents",
            params={"limit": limit, "offset": offset},
        )

    def get_document(self, namespace_id: str, document_id: str) -> dict[str, Any]:
        return self._request("GET", f"/namespaces/{namespace_id}/documents/{document_id}")

    def list_document_jobs(self, namespace_id: str, document_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/namespaces/{namespace_id}/documents/{document_id}/jobs")

    def reprocess_document(self, namespace_id: str, document_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/documents/{document_id}/reprocess",
            timeout=self._settings.upload_timeout,
        )

    def delete_document(self, namespace_id: str, document_id: str) -> bool:
        self._request("DELETE", f"/namespaces/{namespace_id}/documents/{document_id}")
        return True

    # -------------------------------------------------------------- answers ---
    def search(
        self, namespace_id: str, collection_id: str, *, query: str, top_k: int | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if top_k:
            body["top_k"] = top_k
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/collections/{collection_id}/search",
            json_body=body,
        )

    # -------------------------------------------------------- conversations ---
    def list_conversations(self, namespace_id: str, *, limit: int = 50) -> dict[str, Any]:
        return self._request(
            "GET", f"/namespaces/{namespace_id}/conversations", params={"limit": limit}
        )

    def create_conversation(
        self, namespace_id: str, *, collection_id: str, title: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/namespaces/{namespace_id}/conversations",
            json_body={"collection_id": collection_id, "title": title},
        )

    def rename_conversation(
        self, namespace_id: str, conversation_id: str, *, title: str
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/namespaces/{namespace_id}/conversations/{conversation_id}",
            json_body={"title": title},
        )

    def delete_conversation(self, namespace_id: str, conversation_id: str) -> bool:
        self._request("DELETE", f"/namespaces/{namespace_id}/conversations/{conversation_id}")
        return True

    def list_messages(
        self, namespace_id: str, conversation_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/namespaces/{namespace_id}/conversations/{conversation_id}/messages",
            params={"limit": limit},
        )

    def stream_answer(
        self,
        namespace_id: str,
        conversation_id: str,
        *,
        question: str,
        collection_id: str | None = None,
        top_k: int | None = None,
    ) -> Iterator[StreamEvent]:
        """Yield the turn's events as they arrive.

        A generator rather than a buffered call, because the whole value of
        the SSE endpoint is that the first token appears in about a second
        while the full answer takes ten. Buffering here would throw that away
        and leave the user watching a spinner.

        Transport failures are yielded as an ``error`` event rather than
        raised: to the caller, a stream that dies and a stream that reports a
        provider outage are the same situation and deserve the same handling.
        """
        self._refresh_if_due()
        body: dict[str, Any] = {"question": question}
        if collection_id:
            body["collection_id"] = collection_id
        if top_k:
            body["top_k"] = top_k

        try:
            response = self._http.post(
                self._url(f"/namespaces/{namespace_id}/conversations/{conversation_id}/messages"),
                json=body,
                headers=self._headers({"Accept": "text/event-stream"}),
                stream=True,
                timeout=(self._settings.request_timeout, self._settings.stream_timeout),
            )
        except requests.RequestException as exc:
            yield StreamEvent("error", {"code": "unreachable", "message": str(exc)})
            return

        with response:
            if not response.ok:
                error = self._decode_error(response)
                yield StreamEvent(
                    "error", {"code": error.code, "message": error.message, "status": error.status}
                )
                return
            try:
                yield from _decode_sse(response.iter_lines(decode_unicode=True))
            except requests.RequestException as exc:
                # The stream died mid-answer. The backend has already saved
                # whatever it produced, so this is a display problem - say so
                # and let the user retry.
                yield StreamEvent(
                    "error",
                    {
                        "code": "stream_interrupted",
                        "message": f"The connection dropped while streaming: {exc}",
                    },
                )


def _decode_sse(lines: Iterator[str]) -> Iterator[StreamEvent]:
    """Reassemble events from the wire format.

    Deliberately tolerant in the two ways the grammar requires: a line
    starting with ':' is a keep-alive comment and must be ignored, and an
    event is only complete at a blank line - a `data:` field that has not
    been terminated yet is not an event.
    """
    name = "message"
    data_lines: list[str] = []

    for raw in lines:
        line = raw.rstrip("\r") if raw else ""
        if not line:
            if data_lines:
                yield StreamEvent(name, _decode_data("\n".join(data_lines)))
            name, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_name == "event":
            name = value
        elif field_name == "data":
            data_lines.append(value)

    if data_lines:  # a final event with no trailing blank line
        yield StreamEvent(name, _decode_data("\n".join(data_lines)))


def _decode_data(body: str) -> dict[str, Any]:
    try:
        decoded = json.loads(body)
    except ValueError:
        return {"raw": body}
    return decoded if isinstance(decoded, dict) else {"value": decoded}
