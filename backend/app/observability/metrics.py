"""Prometheus metrics: a `Metrics` object owning one `CollectorRegistry`
plus every `t2s_*` collector, and two `app.core.observer.PipelineObserver`
implementations that write pipeline events into it.

`Metrics` is constructed fresh per `create_app()` call (never at import
time / module scope) so each app -- and each test -- gets its own isolated
registry instead of every instance colliding on `prometheus_client`'s
process-global default registry (`app/main.py` stores the instance on
`app.state.metrics` and mounts it at `/metrics` via
`prometheus_client.make_asgi_app(registry=...)`).

`MetricsObserver` is the `PipelineObserver` that actually records events.
`CompositeObserver` exists so `create_app(observer=...)` can keep an
injected observer (e.g. a test's `RecordingObserver`) fully working *and*
still get metrics recorded, by fanning every call out to both rather than
forcing a choice between the two -- see `app.main.create_app`.

Metric names/labels/bucket boundaries are fixed by
`docs/superpowers/plans/2026-08-23-phase2-cache-queue-metrics.md` (Task 4)
and must not drift from it -- label values are restricted to the
low-cardinality set that plan's Global Constraints specify (`provider`,
`model`, `stage`, `reason`, `outcome`, `cache`, `direction`; never question
text, request id, or SQL).
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.core.observer import PipelineObserver
from app.llm.base import Usage

__all__ = ["CompositeObserver", "Metrics", "MetricsObserver", "set_schema_version"]

#: Buckets for `t2s_llm_request_duration_seconds`, per the Task 4 brief --
#: wide because LLM completions routinely run seconds, not milliseconds.
_LLM_DURATION_BUCKETS = (0.25, 0.5, 1, 2, 4, 8, 16, 30, 60)


class Metrics:
    """One `CollectorRegistry` plus every `t2s_*` collector the app
    reports. `t2s_jobs_queue_depth` is defined here (owned by this module,
    like every other `t2s_*` collector) but is only ever `.set()` by Phase 2
    Task 5's arq queue-depth sampler -- it reads `0` until that task wires
    it up."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.llm_request_duration_seconds = Histogram(
            "t2s_llm_request_duration_seconds",
            "LLM completion latency in seconds, by provider/model/stage.",
            ["provider", "model", "stage"],
            buckets=_LLM_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.llm_tokens_total = Counter(
            "t2s_llm_tokens_total",
            "LLM tokens consumed, by provider/model/direction (prompt|completion).",
            ["provider", "model", "direction"],
            registry=self.registry,
        )
        self.llm_cost_usd_total = Counter(
            "t2s_llm_cost_usd_total",
            "Estimated LLM cost in USD, by provider/model.",
            ["provider", "model"],
            registry=self.registry,
        )
        self.sql_guard_rejections_total = Counter(
            "t2s_sql_guard_rejections_total",
            "SQL guard rejections, by reason.",
            ["reason"],
            registry=self.registry,
        )
        self.sql_execution_total = Counter(
            "t2s_sql_execution_total",
            "SQL execution attempts, by outcome (success|db_error|timeout|rejected).",
            ["outcome"],
            registry=self.registry,
        )
        self.sql_execution_duration_seconds = Histogram(
            "t2s_sql_execution_duration_seconds",
            "SQL execution latency in seconds.",
            registry=self.registry,
        )
        self.cache_requests_total = Counter(
            "t2s_cache_requests_total",
            "Cache read attempts, by cache tier (sql|result|schema) and outcome (hit|miss|bypass).",
            ["cache", "outcome"],
            registry=self.registry,
        )
        self.pipeline_duration_seconds = Histogram(
            "t2s_pipeline_duration_seconds",
            "End-to-end pipeline run latency in seconds, by success (ok=true|false).",
            ["ok"],
            registry=self.registry,
        )
        self.jobs_queue_depth = Gauge(
            "t2s_jobs_queue_depth",
            "Depth of the arq background job queue.",
            registry=self.registry,
        )
        self.schema_version_info = Gauge(
            "t2s_schema_version_info",
            "Currently active schema version (the sample with value 1).",
            ["version"],
            registry=self.registry,
        )


def set_schema_version(metrics: Metrics, version: str) -> None:
    """Mark `version` as the sole currently-active schema version:
    `.clear()` drops every previously-set `t2s_schema_version_info{version}`
    sample (so a refresh that changes the version doesn't leave the old one
    behind reading 1 forever), then the new version is set to 1. Called from
    `app.main`'s lifespan at startup and from `POST /schema/refresh`
    (`app/api/v1/schema.py`) after a new `SchemaGraph` is built."""
    metrics.schema_version_info.clear()
    metrics.schema_version_info.labels(version=version).set(1)


class MetricsObserver:
    """`PipelineObserver` that records every pipeline event onto `metrics`.

    `provider` and the `prices` table are fixed at construction (one
    `MetricsObserver` per app, mirroring one `Metrics` per app) since both
    come from `Settings` and never change mid-request.
    """

    def __init__(
        self, metrics: Metrics, *, provider: str, prices: dict[str, tuple[float, float]]
    ) -> None:
        self._metrics = metrics
        self._provider = provider
        self._prices = prices

    def on_stage(self, stage: str, ms: float) -> None:
        # No `t2s_*` metric tracks generic per-stage timing on its own --
        # `t2s_llm_request_duration_seconds` (via `on_llm`) and
        # `t2s_sql_execution_duration_seconds` (via `on_execution`) already
        # cover the two stages worth a histogram. Stage timings are logged
        # by `app.core.pipeline` itself (`logger.info("stage", ...)`).
        pass

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        metrics = self._metrics
        metrics.llm_request_duration_seconds.labels(
            provider=self._provider, model=model, stage=stage
        ).observe(usage.latency_ms / 1000)
        metrics.llm_tokens_total.labels(
            provider=self._provider, model=model, direction="prompt"
        ).inc(usage.prompt_tokens)
        metrics.llm_tokens_total.labels(
            provider=self._provider, model=model, direction="completion"
        ).inc(usage.completion_tokens)

        price_in, price_out = self._prices.get(model, (0.0, 0.0))
        cost = (usage.prompt_tokens / 1000) * price_in
        cost += (usage.completion_tokens / 1000) * price_out
        metrics.llm_cost_usd_total.labels(provider=self._provider, model=model).inc(cost)

    def on_guard_reject(self, reason: str) -> None:
        self._metrics.sql_guard_rejections_total.labels(reason=reason).inc()

    def on_execution(self, *, outcome: str, ms: float) -> None:
        self._metrics.sql_execution_total.labels(outcome=outcome).inc()
        # `outcome == "rejected"` is synthetic: `Text2SQLPipeline` reports it
        # with `ms=0.0` for a guard rejection that short-circuited execution
        # entirely (never actually ran) -- see
        # `app.core.observer.PipelineObserver.on_execution`'s docstring.
        # Counted above like any other outcome, but excluded here so it
        # doesn't pollute the duration histogram with a fake zero-second
        # sample.
        if outcome != "rejected":
            self._metrics.sql_execution_duration_seconds.observe(ms / 1000)

    def on_cache(self, *, cache: str, outcome: str) -> None:
        # By design, on the SQL-tier fallthrough path
        # (`Text2SQLPipeline._sql_hit` evicting a stale entry after a
        # repairable execution failure and re-running the full path) this
        # counter records `cache="sql", outcome="hit"` even though the
        # eventual HTTP response reports `cache_status="miss"`: the cache
        # *read* really was a hit, it's only the *response* that
        # reclassifies the run once the stale entry turned out to be
        # unusable -- see the "Two-tier cache" section of
        # `app.core.pipeline`'s module docstring.
        self._metrics.cache_requests_total.labels(cache=cache, outcome=outcome).inc()

    def on_pipeline_done(self, *, ms: float, ok: bool) -> None:
        self._metrics.pipeline_duration_seconds.labels(ok="true" if ok else "false").observe(
            ms / 1000
        )


class CompositeObserver:
    """`PipelineObserver` that fans every call out to each observer in
    `observers`, in order. Lets `app.main.create_app(observer=...)` keep an
    injected observer (e.g. a test's `RecordingObserver`) receiving exactly
    the events it always did, while a `MetricsObserver` also gets every
    event -- instead of the caller having to choose one or the other."""

    def __init__(self, observers: list[PipelineObserver]) -> None:
        self._observers = observers

    def on_stage(self, stage: str, ms: float) -> None:
        for observer in self._observers:
            observer.on_stage(stage, ms)

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        for observer in self._observers:
            observer.on_llm(stage=stage, model=model, usage=usage)

    def on_guard_reject(self, reason: str) -> None:
        for observer in self._observers:
            observer.on_guard_reject(reason)

    def on_execution(self, *, outcome: str, ms: float) -> None:
        for observer in self._observers:
            observer.on_execution(outcome=outcome, ms=ms)

    def on_cache(self, *, cache: str, outcome: str) -> None:
        for observer in self._observers:
            observer.on_cache(cache=cache, outcome=outcome)

    def on_pipeline_done(self, *, ms: float, ok: bool) -> None:
        for observer in self._observers:
            observer.on_pipeline_done(ms=ms, ok=ok)
