"""Grounded answer generation.

The model is scripted here (see :class:`tests.fakes.FakeChatProvider`); every
other part of the path is real. That split is the point: what these tests
check is not whether a model writes good prose, but whether this application
can be made to show a citation it cannot substantiate - by a model that
invents one, by a model that cites nothing, or by a tenant asking about
another tenant's documents.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

from app.llm.base import LLMConfigurationError, LLMError, LLMUnavailable
from app.llm.openai_compatible import OpenAICompatibleChatProvider
from app.llm.prompts import INSUFFICIENT_EVIDENCE, REFUSAL_MESSAGE
from tests.conftest import make_collection, make_namespace, signed_in
from tests.fakes import FakeChatProvider
from tests.test_indexing import HANDBOOK, SECURITY, upload_text

LEAVE_QUESTION = "How many days of annual leave do employees get?"


async def ask(
    client: AsyncClient,
    headers: dict[str, str],
    namespace_id: str,
    collection_id: str,
    question: str = LEAVE_QUESTION,
    **body: Any,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/namespaces/{namespace_id}/collections/{collection_id}/answer",
        headers=headers,
        json={"question": question, **body},
    )


@pytest.fixture
async def owner(client: AsyncClient) -> dict[str, str]:
    return await signed_in(client, "owner@example.com")


@pytest.fixture
async def workspace(client: AsyncClient, owner: dict[str, str]) -> dict[str, str]:
    """One namespace, one collection, two indexed documents."""
    namespace = await make_namespace(client, owner, "acme")
    collection = await make_collection(client, owner, namespace["id"], "policies")
    handbook = await upload_text(
        client,
        owner,
        namespace["id"],
        collection["id"],
        filename="handbook.txt",
        body=HANDBOOK,
        title="Employee Handbook",
    )
    security = await upload_text(
        client,
        owner,
        namespace["id"],
        collection["id"],
        filename="security.txt",
        body=SECURITY,
    )
    return {
        "namespace_id": namespace["id"],
        "collection_id": collection["id"],
        "handbook_id": handbook["id"],
        "security_id": security["id"],
    }


@pytest.fixture
async def empty_workspace(client: AsyncClient, owner: dict[str, str]) -> dict[str, str]:
    namespace = await make_namespace(client, owner, "empty-co")
    collection = await make_collection(client, owner, namespace["id"], "untouched")
    return {"namespace_id": namespace["id"], "collection_id": collection["id"]}


class TestCitationStructure:
    async def test_a_citation_carries_name_excerpt_and_marker(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days [1]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["citations"]) == 1
        citation = body["citations"][0]
        assert citation["marker"] == 1
        assert citation["document_name"]
        assert citation["excerpt"]
        assert uuid.UUID(citation["chunk_id"])
        assert uuid.UUID(citation["document_id"])
        assert f"[{citation['marker']}]" in body["answer"]

    async def test_the_excerpt_is_the_real_passage_not_model_prose(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """A reader has to be able to check the claim against the source."""
        llm.replies = ["Twenty-five days, allegedly [1]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        excerpt = response.json()["citations"][0]["excerpt"]
        assert excerpt in HANDBOOK or excerpt in SECURITY
        assert "allegedly" not in excerpt

    async def test_a_document_title_is_preferred_over_its_filename(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Twenty-five days [1]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.json()["citations"][0]["document_name"] == "Employee Handbook"

    async def test_a_page_is_null_rather_than_invented(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """Plain text has no pages; a fabricated page 1 would be worse than none."""
        llm.replies = ["Twenty-five days [1]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.json()["citations"][0]["page_number"] is None

    async def test_only_the_passages_the_model_cited_are_returned(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """Returning everything retrieved would overstate what the answer rests on."""
        llm.replies = ["Only the second passage matters here [2]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        body = response.json()
        assert body["retrieved_count"] >= 2
        assert [c["marker"] for c in body["citations"]] == [2]

    async def test_a_grouped_marker_yields_one_citation_each(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Both passages agree [1, 2]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert [c["marker"] for c in response.json()["citations"]] == [1, 2]

    async def test_a_marker_repeated_in_the_prose_is_cited_once(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["First claim [1]. Second claim, same source [1]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert [c["marker"] for c in response.json()["citations"]] == [1]


class TestCitationsCannotBeInvented:
    async def test_an_out_of_range_marker_is_dropped(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """The model cites a ninth passage that was never supplied."""
        llm.replies = ["Twenty-five days [1]. Also, sabbaticals are unlimited [9]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        body = response.json()
        assert [c["marker"] for c in body["citations"]] == [1]
        # Removed from the prose too: showing [9] would promise a source the
        # response cannot produce.
        assert "[9]" not in body["answer"]

    async def test_a_wholly_invented_citation_leaves_no_trace(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get forty days [7]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        body = response.json()
        assert body["citations"] == []
        assert "[7]" not in body["answer"]
        # The answer survives, but is reported as unsupported rather than
        # dressed up with a citation it did not earn.
        assert body["grounded"] is False

    async def test_mixed_valid_and_invalid_markers_keep_only_the_valid(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Both of these are true [1, 42]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        body = response.json()
        assert [c["marker"] for c in body["citations"]] == [1]
        assert "[42]" not in body["answer"]
        assert "[1]" in body["answer"]

    async def test_zero_is_not_a_passage(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """Markers are 1-based; [0] is off by one, not a citation."""
        llm.replies = ["Something [0]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.json()["citations"] == []

    async def test_an_uncited_answer_is_reported_as_ungrounded(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days of leave."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        body = response.json()
        assert body["citations"] == []
        assert body["grounded"] is False
        # Still "sufficient evidence": the model did not refuse, it simply
        # failed to cite. Conflating the two would hide a prompt regression.
        assert body["has_sufficient_evidence"] is True


class TestInsufficientEvidence:
    async def test_the_model_can_refuse(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = [
            f"{INSUFFICIENT_EVIDENCE} The passages cover leave and security, not pensions."
        ]

        response = await ask(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            "What is the employer pension contribution?",
        )

        body = response.json()
        assert body["has_sufficient_evidence"] is False
        assert body["citations"] == []
        assert REFUSAL_MESSAGE in body["answer"]
        # The sentinel is an internal protocol token, not something to show.
        assert INSUFFICIENT_EVIDENCE not in body["answer"]

    async def test_the_models_explanation_is_kept(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = [f"{INSUFFICIENT_EVIDENCE}: nothing here mentions pensions."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert "nothing here mentions pensions" in response.json()["answer"]

    async def test_a_refusal_discards_any_citations_it_came_with(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """A refusal citing sources would be incoherent; the refusal wins."""
        llm.replies = [f"{INSUFFICIENT_EVIDENCE} nothing relevant [1]."]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        body = response.json()
        assert body["citations"] == []
        assert body["has_sufficient_evidence"] is False

    async def test_nothing_retrieved_means_the_model_is_never_called(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        empty_workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """No passages means no grounded answer is possible, so no spend."""
        response = await ask(
            client,
            owner,
            empty_workspace["namespace_id"],
            empty_workspace["collection_id"],
        )

        body = response.json()
        assert response.status_code == 200
        assert llm.calls == 0
        assert body["has_sufficient_evidence"] is False
        assert body["retrieved_count"] == 0
        assert body["citations"] == []
        assert body["answer"] == REFUSAL_MESSAGE

    async def test_a_high_min_score_filters_everything_out(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        response = await ask(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            "something entirely unrelated to these documents",
            min_score=0.999,
        )

        assert response.json()["has_sufficient_evidence"] is False
        assert llm.calls == 0


class TestWhatReachesTheModel:
    async def test_only_retrieved_passages_are_in_the_prompt(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        prompt = llm.last_prompt
        assert LEAVE_QUESTION in prompt
        assert "[1]" in prompt

    async def test_another_tenants_text_never_reaches_the_prompt(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """The leak this endpoint most needs to not have."""
        rival = await signed_in(client, "rival@example.com")
        rival_namespace = await make_namespace(client, rival, "globex")
        rival_collection = await make_collection(client, rival, rival_namespace["id"], "policies")
        await upload_text(
            client,
            rival,
            rival_namespace["id"],
            rival_collection["id"],
            filename="theirs.txt",
            body="Globex staff may claim mileage at forty pence per mile.",
        )

        await ask(
            client,
            rival,
            rival_namespace["id"],
            rival_collection["id"],
            # Aimed squarely at the other tenant's handbook, without quoting
            # it - a question that quoted it would put the text in the prompt
            # by itself and prove nothing.
            "How many days of annual leave do employees accrue each year?",
        )

        prompt = llm.last_prompt
        # A phrase that exists only in the other tenant's document.
        assert "thirty-first of March" not in prompt
        # The rival's own document did reach the prompt, so the absence above
        # is isolation rather than an empty context.
        assert "forty pence per mile" in prompt

    async def test_top_k_bounds_what_is_sent(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        response = await ask(
            client, owner, workspace["namespace_id"], workspace["collection_id"], top_k=1
        )

        assert response.json()["retrieved_count"] == 1
        assert "[2]" not in llm.last_prompt

    async def test_model_settings_come_from_configuration(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
        settings: Any,
    ) -> None:
        await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        request = llm.requests[-1]
        assert request.temperature == settings.llm_temperature
        assert request.max_tokens == settings.llm_max_tokens

    async def test_a_client_cannot_choose_the_model(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """Otherwise the endpoint is an open proxy to every model on the router."""
        response = await ask(
            client,
            owner,
            workspace["namespace_id"],
            workspace["collection_id"],
            model="openai/gpt-5.5-pro",
            temperature=1.9,
        )

        assert response.status_code == 200
        assert response.json()["model"] == llm.model
        assert llm.requests[-1].temperature != 1.9

    async def test_the_system_prompt_forbids_outside_knowledge(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        system = llm.requests[-1].messages[0]
        assert system.role == "system"
        assert INSUFFICIENT_EVIDENCE in system.content


class TestGenerationFailures:
    @pytest.mark.parametrize(
        ("error", "expected_code"),
        [
            (LLMUnavailable("provider down"), "service_unavailable"),
            (LLMConfigurationError("no such model"), "upstream_misconfigured"),
            (LLMError("router returned no completion choices"), "service_unavailable"),
        ],
    )
    async def test_a_provider_failure_becomes_503(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
        error: Exception,
        expected_code: str,
    ) -> None:
        llm.error = error

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.status_code == 503
        assert response.json()["error"]["code"] == expected_code

    async def test_a_failure_does_not_leak_the_providers_wording(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        """Upstream errors quote the request back, which can include the prompt."""
        llm.error = LLMConfigurationError(
            "Invalid API key sk-or-v1-secret for org acme; prompt was: ..."
        )

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert "sk-or-v1-secret" not in response.text
        assert "prompt was" not in response.text

    async def test_an_empty_completion_is_an_error_not_an_empty_answer(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["   "]

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.status_code == 503

    async def test_a_truncated_answer_says_so(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        llm.replies = ["Employees get twenty-five days [1] and also"]
        llm.finish_reason = "length"

        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        assert response.json()["truncated"] is True

    async def test_token_usage_is_reported(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        response = await ask(client, owner, workspace["namespace_id"], workspace["collection_id"])

        usage = response.json()["usage"]
        assert usage["prompt_tokens"] == 120
        assert usage["completion_tokens"] == 30
        assert usage["total_tokens"] == 150


class TestAccessControl:
    async def test_an_anonymous_request_is_rejected(
        self, client: AsyncClient, workspace: dict[str, str], llm: FakeChatProvider
    ) -> None:
        response = await client.post(
            f"/api/v1/namespaces/{workspace['namespace_id']}"
            f"/collections/{workspace['collection_id']}/answer",
            json={"question": LEAVE_QUESTION},
        )

        assert response.status_code == 401
        assert llm.calls == 0

    async def test_a_non_member_gets_404_and_costs_nothing(
        self, client: AsyncClient, workspace: dict[str, str], llm: FakeChatProvider
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await ask(
            client, outsider, workspace["namespace_id"], workspace["collection_id"]
        )

        assert response.status_code == 404
        assert llm.calls == 0

    async def test_an_unknown_collection_is_404(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
    ) -> None:
        response = await ask(client, owner, workspace["namespace_id"], str(uuid.uuid4()))

        assert response.status_code == 404
        assert llm.calls == 0

    @pytest.mark.parametrize("question", ["", "   ", "x" * 5_000])
    async def test_an_unusable_question_is_rejected_before_the_model(
        self,
        client: AsyncClient,
        owner: dict[str, str],
        workspace: dict[str, str],
        llm: FakeChatProvider,
        question: str,
    ) -> None:
        response = await ask(
            client, owner, workspace["namespace_id"], workspace["collection_id"], question
        )

        assert response.status_code == 422
        assert llm.calls == 0


# ----------------------------------------------------- the provider itself ---
def _http_response(status: int) -> httpx.Response:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(status, request=request)


class StubCompletions:
    """Stands in for ``client.chat.completions``."""

    def __init__(self, *, raises: Exception | None = None, result: Any = None) -> None:
        self.raises = raises
        self.result = result
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


def install(provider: OpenAICompatibleChatProvider, stub: StubCompletions) -> StubCompletions:
    provider._client.chat.completions = stub  # type: ignore[assignment]
    return stub


def build_provider(**kwargs: Any) -> OpenAICompatibleChatProvider:
    return OpenAICompatibleChatProvider(
        "anthropic/claude-sonnet-5.5",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_attempts=kwargs.pop("max_attempts", 2),
        **kwargs,
    )


class TestProviderErrorMapping:
    """The OpenRouter client's classification of SDK errors.

    Mocked at the SDK boundary rather than over HTTP: what matters is which
    exception type maps to which of our own, and whether it is retried.
    """

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def instant(_seconds: float) -> None:
            return None

        monkeypatch.setattr("app.core.retry.asyncio.sleep", instant)

    async def _call(self, provider: OpenAICompatibleChatProvider) -> Any:
        from app.llm.base import ChatMessage, ChatRequest

        return await provider.complete(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")])
        )

    async def test_a_missing_model_is_a_configuration_error(self) -> None:
        """How a router says "no such model, or not enabled for you"."""
        provider = build_provider()
        stub = install(
            provider,
            StubCompletions(
                raises=NotFoundError("no such model", response=_http_response(404), body=None)
            ),
        )

        with pytest.raises(LLMConfigurationError, match="LLM_MODEL"):
            await self._call(provider)

        assert stub.calls == 1  # not retried

    async def test_a_rejected_key_is_not_retried(self) -> None:
        provider = build_provider()
        stub = install(
            provider,
            StubCompletions(
                raises=AuthenticationError("bad key", response=_http_response(401), body=None)
            ),
        )

        with pytest.raises(LLMConfigurationError, match="credentials"):
            await self._call(provider)

        assert stub.calls == 1

    async def test_a_malformed_request_is_not_retried(self) -> None:
        provider = build_provider()
        stub = install(
            provider,
            StubCompletions(
                raises=BadRequestError("bad request", response=_http_response(400), body=None)
            ),
        )

        with pytest.raises(LLMConfigurationError):
            await self._call(provider)

        assert stub.calls == 1

    @pytest.mark.parametrize(
        "error",
        [
            RateLimitError("slow down", response=_http_response(429), body=None),
            InternalServerError("upstream broke", response=_http_response(500), body=None),
            APITimeoutError(
                request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
            ),
            APIConnectionError(
                request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
            ),
        ],
    )
    async def test_a_transient_failure_is_retried_then_surfaces(self, error: Exception) -> None:
        provider = build_provider(max_attempts=3)
        stub = install(provider, StubCompletions(raises=error))

        with pytest.raises(LLMUnavailable):
            await self._call(provider)

        assert stub.calls == 3

    async def test_a_retry_can_succeed(self) -> None:
        provider = build_provider(max_attempts=3)

        class Flaky(StubCompletions):
            async def create(self, **kwargs: Any) -> Any:
                self.calls += 1
                if self.calls < 3:
                    raise RateLimitError("slow down", response=_http_response(429), body=None)
                return _completion("recovered [1]")

        stub = install(provider, Flaky())

        result = await self._call(provider)

        assert result.content == "recovered [1]"
        assert stub.calls == 3

    async def test_an_empty_choice_list_is_an_error(self) -> None:
        provider = build_provider()
        install(provider, StubCompletions(result=_empty_completion()))

        with pytest.raises(LLMError, match="no completion"):
            await self._call(provider)

    async def test_the_served_model_is_reported_not_the_requested_one(self) -> None:
        """Routers fall back; the response should say what actually answered."""
        provider = build_provider()
        install(provider, StubCompletions(result=_completion("hi", model="anthropic/claude-5")))

        result = await self._call(provider)

        assert result.model == "anthropic/claude-5"


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None, finish_reason: str) -> None:
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 10
        self.completion_tokens = 5


class _Response:
    def __init__(self, choices: list[_Choice], model: str) -> None:
        self.choices = choices
        self.model = model
        self.usage = _Usage()


def _completion(
    content: str, *, model: str = "anthropic/claude-sonnet-5.5", finish_reason: str = "stop"
) -> _Response:
    return _Response([_Choice(content, finish_reason)], model)


def _empty_completion() -> _Response:
    return _Response([], "anthropic/claude-sonnet-5.5")
