"""Guards for defects that only a real PostgreSQL ever showed.

The suite fakes the SQL layer, which buys speed and determinism at a cost
that is easy to forget: a whole class of bug is invisible to it. Every test
here exists because something passed the full suite and then returned 500
against a real database, in a container, by hand.

None of these can re-run the original failure - that needs live PostgreSQL,
which the suite deliberately does not have. What they can do is assert the
*property* each fix established, so a later edit that quietly undoes one
fails here instead of in a deployment. Read each docstring as a description
of a bug that actually happened.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import deps
from app.db.base import Base
from app.db.models.document import Document
from app.db.models.namespace import Namespace
from app.llm.base import ChatRequest, LLMConfigurationError
from app.llm.factory import LazyChatProvider


class TestServerDefaultsAreFetchedEagerly:
    """`updated_at` must not need a second trip to the database to be read.

    The column carries a server-side ``onupdate=func.now()``. SQLAlchemy
    cannot predict what the database clock will produce, so after any UPDATE
    it marks the attribute expired and defers a refetch to the next access.

    Under asyncio that deferral is fatal rather than merely slow. Responses
    are built by ``Model.model_validate(orm_object)``, and pydantic reads
    attributes from *synchronous* validator code, where a lazy load has no
    greenlet to run in. The result was `MissingGreenlet`, surfacing as a 500
    from every endpoint that updated a row and then returned it: document
    upload, PATCH namespace, PATCH collection.

    `expire_on_commit=False` on the session does not fix it - the expiry is
    set by the flush that issued the UPDATE, not by the commit. Fetching
    server defaults via RETURNING in the same statement does.
    """

    def test_the_base_mapper_fetches_server_defaults_eagerly(self) -> None:
        assert Base.__mapper_args__.get("eager_defaults") is True, (
            "eager_defaults must stay on: without it a server-side onupdate "
            "leaves `updated_at` expired after every UPDATE, and serializing "
            "the row raises MissingGreenlet."
        )

    @pytest.mark.parametrize("model", [Namespace, Document])
    def test_models_inherit_the_eager_fetch(self, model: type) -> None:
        """Inheritance is the mechanism, so assert it rather than assume it.

        A model that declares its own ``__mapper_args__`` silently replaces
        the base mapping rather than merging with it, which would reintroduce
        the bug for that one table only - the hardest version to notice.
        """
        assert model.__mapper__.eager_defaults is True

    def test_updated_at_still_uses_the_database_clock(self) -> None:
        """The fix must not have been "make the timestamp client-side".

        Moving `onupdate` into Python would also stop the expiry, and would
        be wrong: application servers disagree about the time, the database
        does not. The eager fetch keeps the database authoritative.
        """
        column = Namespace.__table__.c.updated_at
        assert column.onupdate is not None
        assert column.onupdate.is_clause_element, (
            "updated_at should be computed by the database, not by the app"
        )


class TestTheChatProviderIsBuiltOnlyWhenUsed:
    """A missing LLM key must not break endpoints that never call a model.

    FastAPI resolves the entire dependency tree before an endpoint body runs.
    `ChatService` is a dependency of every conversation endpoint - listing,
    creating, renaming, deleting - and it asked for a `ChatProvider`. Building
    that provider eagerly meant `LLMConfigurationError` was raised during
    dependency resolution, so a deployment with no key returned 500 from the
    whole conversation surface, not just from answering.
    """

    def test_the_dependency_hands_out_a_lazy_provider(self) -> None:
        assert isinstance(deps.get_llm(), LazyChatProvider)

    def test_constructing_one_touches_no_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Construction alone must not call the factory at all."""

        def explode() -> None:
            raise AssertionError("the provider was built during construction")

        monkeypatch.setattr("app.llm.factory.get_chat_provider", explode)
        LazyChatProvider()  # must not raise

    async def test_the_error_surfaces_on_first_real_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deferred, not suppressed: a genuine call still reports the problem."""

        def explode() -> None:
            raise LLMConfigurationError("LLM_PROVIDER=openrouter needs an API key")

        monkeypatch.setattr("app.llm.factory.get_chat_provider", explode)
        provider = LazyChatProvider()

        with pytest.raises(LLMConfigurationError):
            await provider.complete(ChatRequest(messages=[]))

    def test_stream_is_not_a_coroutine(self) -> None:
        """`stream` must return the iterator without awaiting.

        The caller owns the generator and closes it when a client disconnects,
        which is what cancels the upstream request. Wrapping it in `async def`
        would make the proxy await the whole stream first and break
        disconnect handling for every streamed answer.
        """
        import inspect

        assert not inspect.iscoroutinefunction(LazyChatProvider.stream)


class TestMisconfigurationIsReportedAsUnavailable:
    """An unset key is 503 with a cause, not an opaque 500.

    The answer service maps `LLMConfigurationError` itself, but only for
    errors raised inside a generation call. A lazily built provider can raise
    it from anywhere that first touches the model, where it previously fell
    through to the catch-all handler and was reported as "An unexpected error
    occurred" - sending an operator hunting for a bug instead of a variable.
    """

    async def test_a_configuration_error_becomes_503_with_a_distinct_code(self) -> None:
        from app.core.config import get_settings
        from app.main import create_app

        app: FastAPI = create_app(get_settings())

        @app.get("/boom-config")
        async def boom() -> None:
            raise LLMConfigurationError("needs an API key")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/boom-config")

        assert response.status_code == 503
        body = response.json()["error"]
        assert body["code"] == "upstream_misconfigured"
        assert "api key" not in body["message"].lower(), (
            "the operator-facing cause belongs in the logs, not in the response"
        )

    async def test_the_response_carries_a_correlation_id(self) -> None:
        """So the opaque message can be joined to the log line that explains it."""
        from app.core.config import get_settings
        from app.main import create_app

        app: FastAPI = create_app(get_settings())

        @app.get("/boom-config")
        async def boom() -> None:
            raise LLMConfigurationError("needs an API key")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/boom-config")

        assert response.json()["error"]["request_id"]
        assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


class TestAFailedDocumentCanBeDeleted:
    """Deleting a document that failed to process must not be refused.

    `ck_documents_error_message_matches_status` was a biconditional:

        (status = 'failed') = (error_message IS NOT NULL)

    It says two things, and only the first is right. A failed document must
    explain itself - yes. Nothing else may carry an error - too strong, because
    deletion moves a document to `deleting` and then `archived` while leaving
    `error_message` in place. For a document that had failed, that UPDATE
    violated the CHECK, so `DELETE` returned 500 and **no failed document
    could ever be removed**: a scanned PDF that could not be OCR'd was
    unremovable for the life of the deployment.

    Invisible to the suite because the fakes do not enforce CHECK constraints.
    Found by a user clicking Delete, reproduced against a real PostgreSQL,
    fixed in migration `0004`.
    """

    @staticmethod
    def _condition() -> str:
        from sqlalchemy import CheckConstraint

        # Matched by suffix: the metadata naming convention prefixes it to
        # `ck_documents_...`, and that prefix is applied at table-definition
        # time rather than being the name written in the model.
        for constraint in Document.__table__.constraints:
            if isinstance(constraint, CheckConstraint) and str(constraint.name).endswith(
                "error_message_matches_status"
            ):
                return " ".join(str(constraint.sqltext).split()).lower()
        raise AssertionError("the error_message/status constraint is missing entirely")

    def test_the_tombstone_states_are_exempt(self) -> None:
        """`deleting` and `archived` must not be governed by the rule."""
        condition = self._condition()
        for state in ("deleting", "archived"):
            assert f"when '{state}' then true" in condition, (
                f"{state!r} must be exempt, or deleting a failed document "
                f"violates the constraint and returns 500"
            )

    def test_a_failed_document_must_still_explain_itself(self) -> None:
        condition = self._condition()
        assert "when 'failed' then error_message is not null" in condition

    def test_a_live_document_must_still_not_carry_a_stale_error(self) -> None:
        """The half of the old rule that was correct is still enforced.

        Without an ELSE branch the constraint would permit a `ready` document
        carrying an error from a previous attempt, which is a real defect and
        the reason this constraint was narrowed rather than dropped.
        """
        condition = self._condition()
        assert "else error_message is null" in condition

    def test_the_old_biconditional_is_gone(self) -> None:
        condition = self._condition()
        assert "=" not in condition.replace("is not null", "").replace("is null", ""), (
            "the biconditional form is what made failed documents undeletable"
        )
