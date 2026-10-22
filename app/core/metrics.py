"""In-process operational metrics, exposed in Prometheus text format.

**Why not ``prometheus_client``?** It is the right answer for a system that
needs multiprocess collection, exemplars or a push gateway. What this project
needs is four instrument types and a ``/metrics`` endpoint, and writing them
here makes the cardinality rules explicit rather than leaving them to
whoever adds the next label.

**The cardinality rule, stated once.** A label value must come from a closed
set the code controls: an HTTP method, a route *template* (``/documents/{id}``,
never the id itself), a job kind, an outcome. Never a user id, a namespace id,
a document id or a raw path. Each distinct combination is a time series held
for the life of the process and stored forever by the scraper, so one
unbounded label is the difference between a few hundred series and a few
million.

**What this is not.** Counters live in the process. Two replicas hold two
halves of the truth, which is exactly what Prometheus expects - it scrapes
each instance and sums across them. A restart resets the counters, which is
also expected: ``rate()`` is defined over counter resets. But it does mean
these numbers are not a billing ledger. The durable record of what happened
is the audit table and the job rows, not this.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

#: Seconds. Chosen for the shape of this workload rather than copied from a
#: default: an in-memory search finishes inside 100 ms, a model call takes
#: seconds, and a large PDF takes tens of seconds. Buckets that stop at 10
#: would put every ingestion in ``+Inf`` and make the histogram useless for
#: the one operation whose latency is actually in question.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.025,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)

Labels = tuple[tuple[str, str], ...]


def _labels(values: dict[str, str] | None) -> Labels:
    """Normalise a label dict into a hashable, order-independent key."""
    if not values:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in values.items()))


def _render_labels(labels: Labels, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = labels + extra
    if not pairs:
        return ""
    body = ",".join(f'{key}="{_escape(value)}"' for key, value in pairs)
    return "{" + body + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class _Counter:
    name: str
    help: str
    values: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))

    def add(self, amount: float, labels: Labels) -> None:
        self.values[labels] += amount

    def render(self) -> Iterator[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        for labels, value in sorted(self.values.items()):
            yield f"{self.name}{_render_labels(labels)} {value:g}"


@dataclass
class _Gauge:
    name: str
    help: str
    values: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))

    def set(self, value: float, labels: Labels) -> None:
        self.values[labels] = value

    def add(self, amount: float, labels: Labels) -> None:
        self.values[labels] += amount

    def render(self) -> Iterator[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        for labels, value in sorted(self.values.items()):
            yield f"{self.name}{_render_labels(labels)} {value:g}"


@dataclass
class _Histogram:
    name: str
    help: str
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    counts: dict[Labels, list[int]] = field(default_factory=dict)
    sums: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))
    totals: dict[Labels, int] = field(default_factory=lambda: defaultdict(int))

    def observe(self, value: float, labels: Labels) -> None:
        # Buckets are stored already cumulative - an observation lands in
        # every bucket whose upper bound it is under, which is exactly what
        # `le="0.1"` means in the exposition. Storing per-bucket counts and
        # summing at render time would give the same answer, but only if
        # nobody ever accumulates twice; this way there is one place for the
        # rule to live.
        counts = self.counts.setdefault(labels, [0] * len(self.buckets))
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                counts[index] += 1
        self.sums[labels] += value
        self.totals[labels] += 1

    def render(self) -> Iterator[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        for labels in sorted(self.counts):
            counts = self.counts[labels]
            for bound, count in zip(self.buckets, counts, strict=True):
                le = ("le", f"{bound:g}")
                yield f"{self.name}_bucket{_render_labels(labels, (le,))} {count}"
            # +Inf is the total by definition: every observation is under it,
            # including the ones that overflowed the last finite bucket.
            total = self.totals[labels]
            yield f"{self.name}_bucket{_render_labels(labels, (('le', '+Inf'),))} {total}"
            yield f"{self.name}_sum{_render_labels(labels)} {self.sums[labels]:g}"
            yield f"{self.name}_count{_render_labels(labels)} {total}"


class MetricsRegistry:
    """The process-wide instrument set.

    Guarded by a lock because metrics are written from the event loop *and*
    from ``asyncio.to_thread`` workers (Chroma calls are blocking). The
    critical sections are a dictionary update each, so contention is not a
    realistic concern; losing an increment to a torn read would be.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}

    # ------------------------------------------------------------ writing ---
    def counter(self, name: str, help_text: str, **labels: str) -> None:
        with self._lock:
            instrument = self._counters.get(name)
            if instrument is None:
                instrument = self._counters[name] = _Counter(name, help_text)
            instrument.add(1.0, _labels(labels))

    def increment(self, name: str, amount: float, help_text: str, **labels: str) -> None:
        with self._lock:
            instrument = self._counters.get(name)
            if instrument is None:
                instrument = self._counters[name] = _Counter(name, help_text)
            instrument.add(amount, _labels(labels))

    def gauge(self, name: str, value: float, help_text: str, **labels: str) -> None:
        with self._lock:
            instrument = self._gauges.get(name)
            if instrument is None:
                instrument = self._gauges[name] = _Gauge(name, help_text)
            instrument.set(value, _labels(labels))

    def gauge_add(self, name: str, amount: float, help_text: str, **labels: str) -> None:
        with self._lock:
            instrument = self._gauges.get(name)
            if instrument is None:
                instrument = self._gauges[name] = _Gauge(name, help_text)
            instrument.add(amount, _labels(labels))

    def observe(
        self,
        name: str,
        value: float,
        help_text: str,
        *,
        buckets: Sequence[float] | None = None,
        **labels: str,
    ) -> None:
        with self._lock:
            instrument = self._histograms.get(name)
            if instrument is None:
                instrument = self._histograms[name] = _Histogram(
                    name, help_text, tuple(buckets) if buckets else DEFAULT_BUCKETS
                )
            instrument.observe(value, _labels(labels))

    @contextmanager
    def timer(self, name: str, help_text: str, **labels: str) -> Iterator[None]:
        """Time a block, recording it even when the block raises.

        ``perf_counter`` rather than ``time()``: this measures a duration,
        and a wall clock that is adjusted mid-operation produces a negative
        one.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, help_text, **labels)

    # ------------------------------------------------------------ reading ---
    def render(self) -> str:
        """The full exposition, as Prometheus text format 0.0.4."""
        with self._lock:
            lines: list[str] = []
            for group in (self._counters, self._gauges, self._histograms):
                for name in sorted(group):
                    lines.extend(group[name].render())  # type: ignore[attr-defined]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop every series. For tests; never called by the application."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


#: The module-level registry. A singleton because a metric is a property of
#: the process, and passing a registry through every layer would be the same
#: ceremony the logging context variable avoids.
metrics = MetricsRegistry()


# --------------------------------------------------------------- shortcuts ---
# Named helpers rather than raw `metrics.counter(...)` calls scattered about,
# so the metric names and help strings are defined exactly once and a typo
# cannot silently create a second, near-identical series.


def record_request(method: str, route: str, status_code: int, duration: float) -> None:
    """One HTTP request. ``route`` must be the template, never the raw path."""
    status_class = f"{status_code // 100}xx"
    metrics.counter(
        "rag_http_requests_total",
        "HTTP requests by method, route template and status class.",
        method=method,
        route=route,
        status=str(status_code),
        status_class=status_class,
    )
    metrics.observe(
        "rag_http_request_duration_seconds",
        duration,
        "HTTP request latency in seconds.",
        method=method,
        route=route,
    )


def record_document_ingested(
    outcome: str, *, chunks: int = 0, duration: float | None = None
) -> None:
    metrics.counter(
        "rag_documents_processed_total",
        "Documents whose ingestion reached a terminal state.",
        outcome=outcome,
    )
    if chunks:
        metrics.increment(
            "rag_chunks_indexed_total", float(chunks), "Chunks written to the vector store."
        )
    if duration is not None:
        metrics.observe(
            "rag_document_ingestion_duration_seconds",
            duration,
            "End-to-end ingestion latency per document.",
            outcome=outcome,
        )


def record_job(kind: str, outcome: str, duration: float | None = None) -> None:
    metrics.counter(
        "rag_jobs_total",
        "Background jobs by kind and outcome.",
        kind=kind,
        outcome=outcome,
    )
    if duration is not None:
        metrics.observe(
            "rag_job_duration_seconds",
            duration,
            "Background job latency in seconds.",
            kind=kind,
            outcome=outcome,
        )


def record_retrieval(hits: int, duration: float) -> None:
    metrics.observe(
        "rag_retrieval_duration_seconds",
        duration,
        "Vector search latency in seconds.",
    )
    metrics.observe(
        "rag_retrieval_hits",
        float(hits),
        "Passages returned per search.",
        buckets=(0, 1, 2, 3, 5, 8, 13, 21, 50),
    )


def record_llm_call(model: str, outcome: str, duration: float, tokens: int = 0) -> None:
    """One completion. ``model`` is configuration, so its cardinality is bounded."""
    metrics.counter(
        "rag_llm_requests_total",
        "Completion requests by model and outcome.",
        model=model,
        outcome=outcome,
    )
    metrics.observe(
        "rag_llm_duration_seconds",
        duration,
        "Completion latency in seconds, including streaming to the last token.",
        model=model,
    )
    if tokens:
        metrics.increment(
            "rag_llm_tokens_total",
            float(tokens),
            "Tokens reported by the provider.",
            model=model,
        )
