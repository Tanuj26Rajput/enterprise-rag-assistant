"""Conversations, ownership, and the streaming chat endpoint.

The SSE tests parse the real event stream off the wire rather than calling
the service directly: the framing, the ordering of events and the contents of
the final event are the contract a frontend codes against, and none of that
is visible from inside the service.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app.llm.base import LLMConfigurationError, LLMUnavailable
from app.llm.prompts import INSUFFICIENT_EVIDENCE, REFUSAL_MESSAGE
from tests.conftest import add_member, make_collection, make_namespace, signed_in
from tests.fakes import FakeChatProvider, FakeStore
from tests.test_indexing import HANDBOOK, SECURITY, upload_text

QUESTION = "How many days of annual leave do employees get?"


# ------------------------------------------------------------------ helpers ---
def parse_sse(payload: str) -> list[tuple[str, Any]]:
    """Parse a text/event-stream body into (event, data) pairs."""
    events: list[tuple[str, Any]] = []
    for block in payload.split("\n\n"):
        name: str | None = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data.append(line.removeprefix("data: "))
            elif line.startswith(":"):
                continue  # keep-alive comment
        if name is not None:
            events.append((name, json.loads("\n".join(data)) if data else None))
    return events


def names(events: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in events]


def first(events: list[tuple[str, Any]], name: str) -> Any:
    for event_name, data in events:
        if event_name == name:
            return data
    raise AssertionError(f"no {name!r} event in {names(events)}")


def streamed_text(events: list[tuple[str, Any]]) -> str:
    return "".join(data["delta"] for name, data in events if name == "token")


async def chat(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    conversation_id: str,
    question: str = QUESTION,
    **body: Any,
) -> list[tuple[str, Any]]:
    response = await client.post(
        f"/api/v1/namespaces/{namespace_id}/conversations/{conversation_id}/messages",
        headers=headers,
        json={"question": question, **body},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


async def start_conversation(
    client: AsyncClient, headers: dict[str, str], namespace_id: str, collection_id: str, **body: Any
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/namespaces/{namespace_id}/conversations",
        headers=headers,
        json={"collection_id": collection_id, **body},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def drive_until_disconnect(
    app: Any,
    *,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any],
    disconnect_after: int,
) -> list[bytes]:
    """Call the ASGI app, then send ``http.disconnect`` mid-response.

    ``receive`` hands over the request body once, then blocks until the
    response has produced ``disconnect_after`` chunks, at which point it
    reports the client as gone. Starlette's own disconnect listener consumes
    that message and cancels the response task, which closes the generator -
    the same sequence a dropped TCP connection produces.
    """
    import anyio

    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            *[(k.lower().encode(), v.encode()) for k, v in headers.items()],
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }

    chunks: list[bytes] = []
    gone = anyio.Event()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                chunks.append(chunk)
            if len(chunks) >= disconnect_after:
                gone.set()

    with anyio.move_on_after(10):
        await app(scope, receive, send)
    return chunks


# ----------------------------------------------------------------- fixtures ---
@pytest.fixture
async def owner(client: AsyncClient) -> dict[str, str]:
    return await signed_in(client, "owner@example.com")


@pytest.fixture
async def workspace(client: AsyncClient, owner: dict[str, str]) -> dict[str, str]:
    namespace = await make_namespace(client, owner, "acme")
    collection = await make_collection(client, owner, namespace["id"], "policies")
    await upload_text(
        client,
        owner,
        namespace["id"],
        collection["id"],
        filename="handbook.txt",
        body=HANDBOOK,
        title="Employee Handbook",
    )
    await upload_text(
        client,
        owner,
        namespace["id"],
        collection["id"],
        filename="security.txt",
        body=SECURITY,
    )
    return {"namespace_id": namespace["id"], "collection_id": collection["id"]}


@pytest.fixture
async def conversation(
    client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
) -> dict[str, Any]:
    return await start_conversation(
        client, owner, workspace["namespace_id"], workspace["collection_id"]
    )


class TestConversationLifecycle:
    async def test_a_conversation_can_be_started(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        body = await start_conversation(
            client, owner, workspace["namespace_id"], workspace["collection_id"], title="Leave"
        )

        assert body["title"] == "Leave"
        assert body["message_count"] == 0
        assert body["collection_ids"] == [workspace["collection_id"]]

    async def test_the_owning_user_is_not_echoed_back(self, conversation: dict[str, Any]) -> None:
        """A thread is only ever returned to its owner, so the field is noise."""
        assert "user_id" not in conversation

    async def test_an_unknown_collection_is_rejected(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations",
            headers=owner,
            json={"collection_id": str(uuid.uuid4())},
        )

        assert response.status_code == 404

    async def test_a_collection_from_another_namespace_is_rejected(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        other = await make_namespace(client, owner, "second-co")
        other_collection = await make_collection(client, owner, other["id"], "elsewhere")

        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations",
            headers=owner,
            json={"collection_id": other_collection["id"]},
        )

        assert response.status_code == 404

    async def test_conversations_are_listed_newest_first(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> None:
        first_thread = await start_conversation(
            client, owner, workspace["namespace_id"], workspace["collection_id"], title="One"
        )
        second = await start_conversation(
            client, owner, workspace["namespace_id"], workspace["collection_id"], title="Two"
        )
        await chat(client, owner, workspace["namespace_id"], first_thread["id"])

        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations", headers=owner
        )

        body = response.json()
        assert body["total"] == 2
        # The one that was just used sorts above the one merely created later.
        assert body["items"][0]["id"] == first_thread["id"]
        assert body["items"][1]["id"] == second["id"]

    async def test_a_conversation_can_be_renamed(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
    ) -> None:
        response = await client.patch(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}",
            headers=owner,
            json={"title": "Annual leave questions"},
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Annual leave questions"

    async def test_the_first_question_becomes_the_title(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
    ) -> None:
        await chat(client, owner, workspace["namespace_id"], conversation["id"])

        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}",
            headers=owner,
        )

        assert response.json()["title"] == QUESTION

    async def test_a_deleted_conversation_is_gone(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
    ) -> None:
        url = f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}"

        assert (await client.delete(url, headers=owner)).status_code == 204
        assert (await client.get(url, headers=owner)).status_code == 404

    async def test_streaming_into_a_deleted_conversation_is_404(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        await client.delete(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}",
            headers=owner,
        )

        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
            json={"question": QUESTION},
        )

        assert response.status_code == 404
        assert llm.calls == 0


class TestConversationIsolation:
    @pytest.fixture
    async def colleague(
        self, client: AsyncClient, owner: dict[str, str], workspace: dict[str, str]
    ) -> dict[str, str]:
        """Another member of the *same* namespace, with full admin rights."""
        headers = await signed_in(client, "colleague@example.com")
        await add_member(client, owner, workspace["namespace_id"], "colleague@example.com", "admin")
        return headers

    async def test_a_namespace_admin_cannot_read_another_users_thread(
        self,
        client: AsyncClient,
        workspace: dict[str, str],
        conversation: dict[str, Any],
        colleague: dict[str, str],
    ) -> None:
        """Administering documents is not a licence to read chat history."""
        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}",
            headers=colleague,
        )

        assert response.status_code == 404

    async def test_another_users_thread_is_not_listed(
        self,
        client: AsyncClient,
        workspace: dict[str, str],
        conversation: dict[str, Any],
        colleague: dict[str, str],
    ) -> None:
        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations", headers=colleague
        )

        assert response.json()["total"] == 0

    async def test_another_user_cannot_read_the_messages(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        colleague: dict[str, str],
    ) -> None:
        await chat(client, owner, workspace["namespace_id"], conversation["id"])

        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=colleague,
        )

        assert response.status_code == 404

    async def test_another_user_cannot_post_into_the_thread(
        self,
        client: AsyncClient,
        workspace: dict[str, str],
        conversation: dict[str, Any],
        colleague: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=colleague,
            json={"question": QUESTION},
        )

        assert response.status_code == 404
        assert llm.calls == 0

    async def test_another_user_cannot_delete_or_rename_it(
        self,
        client: AsyncClient,
        workspace: dict[str, str],
        conversation: dict[str, Any],
        colleague: dict[str, str],
    ) -> None:
        url = f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}"

        assert (await client.delete(url, headers=colleague)).status_code == 404
        assert (
            await client.patch(url, headers=colleague, json={"title": "theirs"})
        ).status_code == 404

    async def test_a_non_member_sees_nothing(
        self, client: AsyncClient, workspace: dict[str, str], conversation: dict[str, Any]
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}",
            headers=outsider,
        )

        assert response.status_code == 404

    async def test_anonymous_access_is_rejected(
        self, client: AsyncClient, workspace: dict[str, str], conversation: dict[str, Any]
    ) -> None:
        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            json={"question": QUESTION},
        )

        assert response.status_code == 401


class TestStreaming:
    async def test_the_stream_is_well_formed(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days [1]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        assert names(events)[0] == "start"
        assert names(events)[-1] == "done"
        assert "token" in names(events)

    async def test_the_answer_arrives_in_pieces(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days of annual leave [1]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        tokens = [data for name, data in events if name == "token"]
        assert len(tokens) > 1
        assert "twenty-five" in streamed_text(events)

    async def test_the_start_event_reports_the_evidence_count(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
    ) -> None:
        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        start = first(events, "start")
        assert start["conversation_id"] == conversation["id"]
        assert start["retrieved_count"] >= 1

    async def test_the_final_event_carries_the_citations(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """The whole point of the final event."""
        llm.replies = ["Employees get twenty-five days [1]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        done = first(events, "done")
        assert len(done["citations"]) == 1
        citation = done["citations"][0]
        assert citation["marker"] == 1
        assert citation["document_name"] == "Employee Handbook"
        assert citation["excerpt"]
        assert "page_number" in citation
        assert uuid.UUID(citation["chunk_id"])

    async def test_the_final_event_repeats_the_whole_answer(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days [1]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        assert first(events, "done")["answer"] == "Employees get twenty-five days [1]."

    async def test_an_invented_marker_is_stripped_from_the_streamed_answer_too(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """The streaming path must not be a way around citation resolution."""
        llm.replies = ["Twenty-five days [1]. Sabbaticals are unlimited [9]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        done = first(events, "done")
        assert [c["marker"] for c in done["citations"]] == [1]
        assert "[9]" not in done["answer"]

    async def test_a_marker_split_across_deltas_still_resolves(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """Markers are resolved on the assembled text, never per delta."""
        llm.replies = ["Twenty-five days [1]."]
        llm.stream_chunks = ["Twenty-five days ", "[", "1", "]", "."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        done = first(events, "done")
        assert [c["marker"] for c in done["citations"]] == [1]

    async def test_a_refusal_streams_as_a_refusal(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = [f"{INSUFFICIENT_EVIDENCE} nothing here covers pensions."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        done = first(events, "done")
        assert done["has_sufficient_evidence"] is False
        assert done["citations"] == []
        assert REFUSAL_MESSAGE in done["answer"]

    async def test_no_evidence_means_no_upstream_call(
        self, client: AsyncClient, owner: dict[str, str], llm: FakeChatProvider
    ) -> None:
        namespace = await make_namespace(client, owner, "empty-co")
        collection = await make_collection(client, owner, namespace["id"], "untouched")
        thread = await start_conversation(client, owner, namespace["id"], collection["id"])

        events = await chat(client, owner, namespace["id"], thread["id"])

        assert llm.calls == 0
        done = first(events, "done")
        assert done["has_sufficient_evidence"] is False
        assert done["retrieved_count"] == 0
        assert streamed_text(events) == REFUSAL_MESSAGE

    async def test_an_unusable_question_fails_before_the_stream_opens(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """A 422 is only possible before the first byte; so validate first."""
        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
            json={"question": "   "},
        )

        assert response.status_code == 422
        assert llm.calls == 0


class TestStreamPersistence:
    async def test_both_turns_are_stored_in_order(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Twenty-five days [1]."]

        await chat(client, owner, workspace["namespace_id"], conversation["id"])
        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
        )

        items = response.json()["items"]
        assert [m["role"] for m in items] == ["user", "assistant"]
        assert [m["sequence"] for m in items] == [0, 1]
        assert items[0]["content"] == QUESTION
        assert items[1]["content"] == "Twenty-five days [1]."

    async def test_citations_are_stored_with_the_assistant_turn(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """A snapshot, so an old turn stays readable after re-processing."""
        llm.replies = ["Twenty-five days [1]."]

        await chat(client, owner, workspace["namespace_id"], conversation["id"])
        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
        )

        stored = response.json()["items"][1]["citations"]
        assert len(stored) == 1
        assert stored[0]["document_name"] == "Employee Handbook"
        assert stored[0]["excerpt"]

    async def test_the_counters_are_maintained(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        await chat(client, owner, workspace["namespace_id"], conversation["id"])

        response = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}/conversations/{conversation['id']}",
            headers=owner,
        )

        body = response.json()
        assert body["message_count"] == 2
        assert body["total_tokens"] == 150
        assert body["last_message_at"] is not None

    async def test_the_question_survives_an_upstream_failure(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """Written before the first token is requested, precisely for this."""
        llm.error = LLMUnavailable("provider down")

        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
            json={"question": QUESTION},
        )
        messages = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
        )

        assert response.status_code == 200
        items = messages.json()["items"]
        assert items[0]["role"] == "user"
        assert items[0]["content"] == QUESTION


class TestStreamFailures:
    async def test_a_failure_before_the_first_token_is_an_error_event(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        """200 is already on the wire, so the failure has to go in-band."""
        llm.error = LLMUnavailable("provider down")

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        error = first(events, "error")
        assert error["code"] == "service_unavailable"
        assert "done" not in names(events)

    async def test_a_misconfiguration_is_reported_distinctly(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.error = LLMConfigurationError("no such model")

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        assert first(events, "error")["code"] == "upstream_misconfigured"

    async def test_a_failure_mid_stream_keeps_what_arrived(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days [1] and more besides."]
        llm.stream_chunks = ["Employees ", "get ", "twenty-five ", "days [1]", " and more."]
        llm.stream_error = LLMUnavailable("connection reset")
        llm.fail_after_chunks = 3

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])
        messages = await client.get(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
        )

        assert names(events)[-1] == "error"
        assert streamed_text(events) == "Employees get twenty-five "
        # The partial answer is kept, labelled as a failure rather than
        # presented as a complete reply.
        stored = messages.json()["items"][1]
        assert stored["content"] == "Employees get twenty-five"
        assert stored["finish_reason"] == "error"
        assert stored["error"]

    async def test_a_provider_failure_does_not_leak_its_wording(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.error = LLMConfigurationError("Invalid API key sk-or-v1-secret; prompt was ...")

        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages",
            headers=owner,
            json={"question": QUESTION},
        )

        assert "sk-or-v1-secret" not in response.text


class TestClientDisconnect:
    """A reader who goes away must stop costing tokens.

    Driven against the ASGI application directly rather than through
    ``AsyncClient.stream``. In-process there is no backpressure: the transport
    accepts every chunk the application produces as fast as it produces them,
    so a test client that stops reading does not stop the server. Sending a
    real ``http.disconnect`` is the only way to exercise the path that runs in
    production.
    """

    async def test_a_disconnect_stops_the_upstream_stream(
        self,
        app: Any,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.stream_chunks = [f"word{n} " for n in range(200)]

        sent = await drive_until_disconnect(
            app,
            path=(
                f"/api/v1/namespaces/{workspace['namespace_id']}"
                f"/conversations/{conversation['id']}/messages"
            ),
            headers=owner,
            body={"question": QUESTION},
            disconnect_after=3,
        )

        assert sent, "the response never started"
        assert llm.closed is True
        # The upstream stream was cut short rather than drained.
        assert len(llm.emitted) < 200

    async def test_a_disconnect_leaves_the_thread_usable(
        self,
        app: Any,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.stream_chunks = [f"word{n} " for n in range(200)]
        await drive_until_disconnect(
            app,
            path=(
                f"/api/v1/namespaces/{workspace['namespace_id']}"
                f"/conversations/{conversation['id']}/messages"
            ),
            headers=owner,
            body={"question": QUESTION},
            disconnect_after=3,
        )

        llm.stream_chunks = None
        llm.closed = False
        llm.replies = ["A complete answer [1]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        assert first(events, "done")["answer"] == "A complete answer [1]."

    async def test_a_disconnect_does_not_corrupt_the_next_turn(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.stream_chunks = [f"word{n} " for n in range(50)]
        url = (
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/conversations/{conversation['id']}/messages"
        )
        async with client.stream(
            "POST", url, headers=owner, json={"question": QUESTION}
        ) as response:
            async for _line in response.aiter_lines():
                break

        llm.stream_chunks = None
        llm.closed = False
        llm.replies = ["A complete answer [1]."]

        events = await chat(client, owner, workspace["namespace_id"], conversation["id"])

        assert first(events, "done")["answer"] == "A complete answer [1]."


class TestBoundedHistory:
    async def test_prior_turns_are_replayed(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Twenty-five days [1]."]
        await chat(client, owner, workspace["namespace_id"], conversation["id"])

        await chat(
            client,
            owner,
            workspace["namespace_id"],
            conversation["id"],
            "And how far in advance must it be requested?",
        )

        roles = [m.role for m in llm.requests[-1].messages]
        assert roles == ["system", "user", "assistant", "user"]
        assert llm.requests[-1].messages[1].content == QUESTION

    async def test_history_is_bounded_by_message_count(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
        settings: Any,
    ) -> None:
        """Otherwise the prompt grows until the request fails."""
        for turn in range(8):
            await chat(
                client,
                owner,
                workspace["namespace_id"],
                conversation["id"],
                f"Question number {turn}",
            )

        replayed = llm.requests[-1].messages[1:-1]
        assert len(replayed) <= settings.chat_history_messages
        # It is the *recent* end that is kept: the turn immediately before
        # this one survives, the first turn of the thread does not.
        assert any("Question number 6" in m.content for m in replayed)
        assert not any("Question number 0" in m.content for m in replayed)

    async def test_history_is_bounded_by_characters(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
        settings: Any,
    ) -> None:
        llm.replies = ["x" * 5_000]
        for _ in range(4):
            await chat(client, owner, workspace["namespace_id"], conversation["id"])

        replayed = llm.requests[-1].messages[1:-1]
        assert sum(len(m.content) for m in replayed) <= settings.chat_history_max_chars

    async def test_history_does_not_cross_conversations(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
    ) -> None:
        await chat(
            client,
            owner,
            workspace["namespace_id"],
            conversation["id"],
            "A question in the first thread",
        )

        second = await start_conversation(
            client, owner, workspace["namespace_id"], workspace["collection_id"]
        )
        await chat(
            client,
            owner,
            workspace["namespace_id"],
            second["id"],
            "A question in the second thread",
        )

        replayed = llm.requests[-1].messages[1:-1]
        assert replayed == []

    async def test_stored_citation_markers_are_not_replayed(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        conversation: dict[str, Any],
        llm: FakeChatProvider,
        store: FakeStore,
    ) -> None:
        """Old markers refer to old passages; resending invites reuse."""
        llm.replies = ["Twenty-five days [1]."]
        await chat(client, owner, workspace["namespace_id"], conversation["id"])
        await chat(
            client, owner, workspace["namespace_id"], conversation["id"], "And the notice period?"
        )

        assistant_turns = [m for m in llm.requests[-1].messages if m.role == "assistant"]
        assert assistant_turns
        # The text is replayed; the resolved citation objects are not.
        assert all("excerpt" not in m.content for m in assistant_turns)
