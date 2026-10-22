"""The operational surface: liveness, readiness, metrics and structured logs.

These are the endpoints an orchestrator and a scraper talk to, which makes
them the endpoints nobody notices are broken until an incident. The
distinction the first class tests - liveness must not consult a dependency -
is the one that decides whether a database failover drains traffic or
restarts the fleet.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.logging import ContextFilter, JsonFormatter, request_id_var
from app.core.metrics import MetricsRegistry, record_request


class TestLiveness:
    async def test_health_is_ok_without_consulting_anything(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Liveness with the database on the floor is still 200.

        A liveness probe that fails during a dependency outage tells the
        orchestrator to restart every replica - converting a recoverable
        outage into a crash loop that cannot recover, because the restarted
        process cannot reach the dependency either.
        """

        async def unreachable() -> bool:
            raise AssertionError("liveness must not touch the database")

        monkeypatch.setattr("app.main.check_database_connection", unreachable)

        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_health_needs_no_credentials(self, client: AsyncClient) -> None:
        assert (await client.get("/health")).status_code == 200


class TestReadiness:
    async def test_ready_reports_both_dependencies(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def reachable() -> bool:
            return True

        monkeypatch.setattr("app.main.check_database_connection", reachable)

        response = await client.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["dependencies"] == {"database": "ok", "vector_store": "ok"}

    async def test_an_unreachable_database_is_a_503(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def unreachable() -> bool:
            return False

        monkeypatch.setattr("app.main.check_database_connection", unreachable)

        response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["dependencies"]["database"] == "unreachable"

    async def test_an_unreachable_vector_store_is_a_503(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def reachable() -> bool:
            return True

        async def broken(*_: Any, **__: Any) -> bool:
            return False

        monkeypatch.setattr("app.main.check_database_connection", reachable)
        monkeypatch.setattr("app.vectorstore.chroma.ChromaVectorStore.heartbeat", broken)

        response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["dependencies"]["vector_store"] == "unreachable"

    async def test_a_raising_probe_reports_not_ready_rather_than_500(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe that raises is a probe that tells you nothing.

        The honest answer to "the heartbeat exploded" is not-ready, not an
        internal error - the latter looks to a load balancer like a broken
        endpoint rather than a draining replica.
        """

        async def reachable() -> bool:
            return True

        async def explode(*_: Any, **__: Any) -> bool:
            raise RuntimeError("the persist directory vanished")

        monkeypatch.setattr("app.main.check_database_connection", reachable)
        monkeypatch.setattr("app.vectorstore.chroma.ChromaVectorStore.heartbeat", explode)

        response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"


class TestMetricsEndpoint:
    async def test_the_exposition_parses_as_prometheus_text(self, client: AsyncClient) -> None:
        await client.get("/health")

        response = await client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        for line in response.text.splitlines():
            assert line.startswith("#") or " " in line

    async def test_requests_are_counted_by_route_template_not_by_path(
        self, client: AsyncClient
    ) -> None:
        """The label must be the template or the series count is unbounded.

        One series per document id is how a metrics backend runs out of
        memory, and it is only visible once there are enough documents for it
        to matter.
        """
        await client.get("/api/v1/namespaces/not-a-uuid/collections")
        await client.get("/api/v1/namespaces/also-not-a-uuid/collections")

        body = (await client.get("/metrics")).text

        assert "not-a-uuid" not in body
        assert 'route="/api/v1/namespaces/{namespace_id}/collections"' in body

    async def test_the_scrape_does_not_meter_itself(self, client: AsyncClient) -> None:
        await client.get("/metrics")
        await client.get("/ready")

        body = (await client.get("/metrics")).text

        assert 'route="/metrics"' not in body
        assert 'route="/ready"' not in body

    async def test_build_info_carries_the_version_in_labels(self, client: AsyncClient) -> None:
        body = (await client.get("/metrics")).text

        assert "rag_build_info" in body
        assert 'environment="test"' in body


class TestRegistry:
    def test_histogram_buckets_are_cumulative(self) -> None:
        registry = MetricsRegistry()
        for value in (0.001, 0.2, 7.0):
            registry.observe("t_seconds", value, "help")

        lines = registry.render().splitlines()
        buckets = {
            line.split('le="')[1].split('"')[0]: float(line.rsplit(" ", 1)[1])
            for line in lines
            if line.startswith("t_seconds_bucket")
        }

        assert buckets["0.005"] == 1
        assert buckets["0.25"] == 2
        assert buckets["+Inf"] == 3
        assert "t_seconds_count 3" in registry.render()

    def test_labels_are_order_independent(self) -> None:
        """``{a, b}`` and ``{b, a}`` are one series, not two."""
        registry = MetricsRegistry()
        registry.counter("c_total", "help", method="GET", route="/x")
        registry.counter("c_total", "help", route="/x", method="GET")

        lines = [line for line in registry.render().splitlines() if line.startswith("c_total{")]

        assert len(lines) == 1
        assert lines[0].endswith(" 2")

    def test_a_timer_records_even_when_the_block_raises(self) -> None:
        registry = MetricsRegistry()

        with pytest.raises(ValueError), registry.timer("t_seconds", "help", outcome="error"):
            raise ValueError("boom")

        assert 't_seconds_count{outcome="error"} 1' in registry.render()

    def test_label_values_are_escaped(self) -> None:
        """A quote or a newline in a label would otherwise break the parse."""
        registry = MetricsRegistry()
        registry.counter("c_total", "help", model='we"ird\nname')

        body = registry.render()

        assert '\\"' in body
        assert "\\n" in body
        assert len(body.strip().splitlines()) == 3  # HELP, TYPE, one sample

    def test_status_class_is_recorded_alongside_the_exact_status(self) -> None:
        """Both labels, because they answer different questions.

        `status_class` makes "what is the error rate" one series; the exact
        status makes "is it 429 or 503" answerable without a second metric.
        This writes to the module-level registry, since that is what the
        `record_*` shortcuts are bound to.
        """
        from app.core.metrics import metrics

        record_request("GET", "/x", 503, 0.01)

        body = metrics.render()
        assert 'status="503"' in body
        assert 'status_class="5xx"' in body


class TestStructuredLogging:
    def test_a_record_becomes_one_json_object_per_line(self) -> None:
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "ingested %s", ("a.pdf",), None
        )
        record.document_id = "7f3a"
        ContextFilter().filter(record)

        line = JsonFormatter().format(record)
        payload = json.loads(line)

        assert "\n" not in line
        assert payload["message"] == "ingested a.pdf"
        assert payload["level"] == "INFO"
        assert payload["document_id"] == "7f3a"

    def test_the_ambient_request_id_is_attached_without_being_passed(self) -> None:
        token = request_id_var.set("req-abc123")
        try:
            record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "x", None, None)
            ContextFilter().filter(record)
            payload = json.loads(JsonFormatter().format(record))
        finally:
            request_id_var.reset(token)

        assert payload["request_id"] == "req-abc123"

    def test_a_traceback_stays_on_one_line(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "app.test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )
        ContextFilter().filter(record)

        line = JsonFormatter().format(record)

        assert "\n" not in line
        assert "ValueError: boom" in json.loads(line)["exception"]

    def test_unserialisable_values_do_not_lose_the_line(self) -> None:
        """A log call must never be the thing that raises."""
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "x", None, None)
        record.weird = object()
        ContextFilter().filter(record)

        payload = json.loads(JsonFormatter().format(record))

        assert "object at" in payload["weird"]

    async def test_the_access_log_records_the_template_not_the_path(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="app.main"):
            await client.get("/api/v1/namespaces/11111111-1111-1111-1111-111111111111")

        entry = next(record for record in caplog.records if hasattr(record, "route"))

        assert entry.route == "/api/v1/namespaces/{namespace_id}"
        assert entry.status_code in {401, 403, 404}
        assert isinstance(entry.duration_ms, float)
        assert "11111111" not in entry.route
