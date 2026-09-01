"""Unit tests for `app.observability.metrics`: `MetricsObserver` records
every `PipelineObserver` event onto a `Metrics` registry as the expected
Prometheus samples, `set_schema_version` maintains the schema-version gauge,
and `CompositeObserver` fans events out to every wrapped observer.
"""

from __future__ import annotations

import pytest

from app.llm.base import Usage
from app.observability.metrics import (
    CompositeObserver,
    Metrics,
    MetricsObserver,
    set_schema_version,
)

PRICES = {"gpt-4o-mini": (0.00015, 0.0006)}


def _observer(metrics: Metrics, *, provider: str = "openai") -> MetricsObserver:
    return MetricsObserver(metrics, provider=provider, prices=PRICES)


def test_on_llm_records_duration_tokens_and_cost_for_a_known_model() -> None:
    metrics = Metrics()
    observer = _observer(metrics)

    observer.on_llm(
        stage="generate",
        model="gpt-4o-mini",
        usage=Usage(prompt_tokens=1000, completion_tokens=500, latency_ms=2000.0),
    )

    labels = {"provider": "openai", "model": "gpt-4o-mini", "stage": "generate"}
    assert metrics.registry.get_sample_value(
        "t2s_llm_request_duration_seconds_count", labels
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_llm_request_duration_seconds_sum", labels
    ) == pytest.approx(2.0)

    assert metrics.registry.get_sample_value(
        "t2s_llm_tokens_total",
        {"provider": "openai", "model": "gpt-4o-mini", "direction": "prompt"},
    ) == pytest.approx(1000.0)
    assert metrics.registry.get_sample_value(
        "t2s_llm_tokens_total",
        {"provider": "openai", "model": "gpt-4o-mini", "direction": "completion"},
    ) == pytest.approx(500.0)

    # cost = 1000/1000 * 0.00015 (prompt) + 500/1000 * 0.0006 (completion)
    assert metrics.registry.get_sample_value(
        "t2s_llm_cost_usd_total", {"provider": "openai", "model": "gpt-4o-mini"}
    ) == pytest.approx(0.00045)


def test_on_llm_unknown_model_costs_zero_but_still_records_tokens() -> None:
    metrics = Metrics()
    observer = _observer(metrics)

    observer.on_llm(
        stage="generate",
        model="qwen2.5-coder:7b",
        usage=Usage(prompt_tokens=100, completion_tokens=50, latency_ms=10.0),
    )

    assert metrics.registry.get_sample_value(
        "t2s_llm_cost_usd_total", {"provider": "openai", "model": "qwen2.5-coder:7b"}
    ) == pytest.approx(0.0)
    assert metrics.registry.get_sample_value(
        "t2s_llm_tokens_total",
        {"provider": "openai", "model": "qwen2.5-coder:7b", "direction": "prompt"},
    ) == pytest.approx(100.0)


def test_on_llm_embed_stage_gets_its_own_series() -> None:
    """`SchemaRetriever` reports its embedding calls as `stage="embed"` with
    `completion_tokens=0`; they must land on their own histogram series and
    be priced through the same (usually absent) model entry."""
    metrics = Metrics()
    observer = _observer(metrics)

    observer.on_llm(
        stage="embed",
        model="nomic-embed-text",
        usage=Usage(prompt_tokens=42, completion_tokens=0, latency_ms=250.0),
    )

    embed_labels = {"provider": "openai", "model": "nomic-embed-text", "stage": "embed"}
    assert metrics.registry.get_sample_value(
        "t2s_llm_request_duration_seconds_count", embed_labels
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_llm_request_duration_seconds_sum", embed_labels
    ) == pytest.approx(0.25)
    assert (
        metrics.registry.get_sample_value(
            "t2s_llm_request_duration_seconds_count",
            {"provider": "openai", "model": "nomic-embed-text", "stage": "generate"},
        )
        is None
    )
    assert metrics.registry.get_sample_value(
        "t2s_llm_tokens_total",
        {"provider": "openai", "model": "nomic-embed-text", "direction": "prompt"},
    ) == pytest.approx(42.0)
    # Not in PRICES -- an unpriced embed model costs 0, like any other.
    assert metrics.registry.get_sample_value(
        "t2s_llm_cost_usd_total", {"provider": "openai", "model": "nomic-embed-text"}
    ) == pytest.approx(0.0)


def test_on_guard_reject_increments_rejections_counter_by_reason() -> None:
    metrics = Metrics()
    observer = _observer(metrics)

    observer.on_guard_reject("not_select")
    observer.on_guard_reject("not_select")
    observer.on_guard_reject("unknown_table")

    assert metrics.registry.get_sample_value(
        "t2s_sql_guard_rejections_total", {"reason": "not_select"}
    ) == pytest.approx(2.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_guard_rejections_total", {"reason": "unknown_table"}
    ) == pytest.approx(1.0)


def test_on_execution_counts_every_outcome_but_rejected_is_not_timed() -> None:
    metrics = Metrics()
    observer = _observer(metrics)

    # A guard rejection short-circuits execution -- `ms=0.0` is synthetic
    # (see `PipelineObserver.on_execution`'s docstring) and must not land in
    # the duration histogram, even though it's still counted.
    observer.on_execution(outcome="rejected", ms=0.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_execution_total", {"outcome": "rejected"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value("t2s_sql_execution_duration_seconds_count") == 0.0

    observer.on_execution(outcome="success", ms=15.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_execution_total", {"outcome": "success"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_execution_duration_seconds_count"
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_execution_duration_seconds_sum"
    ) == pytest.approx(0.015)


def test_on_cache_increments_requests_counter_by_cache_and_outcome() -> None:
    metrics = Metrics()
    observer = _observer(metrics)

    observer.on_cache(cache="sql", outcome="hit")
    observer.on_cache(cache="schema", outcome="miss")

    assert metrics.registry.get_sample_value(
        "t2s_cache_requests_total", {"cache": "sql", "outcome": "hit"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_cache_requests_total", {"cache": "schema", "outcome": "miss"}
    ) == pytest.approx(1.0)


def test_on_pipeline_done_records_duration_histogram_by_ok_label() -> None:
    metrics = Metrics()
    observer = _observer(metrics)

    observer.on_pipeline_done(ms=500.0, ok=True)
    observer.on_pipeline_done(ms=100.0, ok=False)

    assert metrics.registry.get_sample_value(
        "t2s_pipeline_duration_seconds_count", {"ok": "true"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_pipeline_duration_seconds_sum", {"ok": "true"}
    ) == pytest.approx(0.5)
    assert metrics.registry.get_sample_value(
        "t2s_pipeline_duration_seconds_count", {"ok": "false"}
    ) == pytest.approx(1.0)


def test_set_schema_version_marks_current_version_and_clears_the_old_one() -> None:
    metrics = Metrics()

    set_schema_version(metrics, "v1")
    assert metrics.registry.get_sample_value(
        "t2s_schema_version_info", {"version": "v1"}
    ) == pytest.approx(1.0)

    set_schema_version(metrics, "v2")
    assert metrics.registry.get_sample_value("t2s_schema_version_info", {"version": "v1"}) is None
    assert metrics.registry.get_sample_value(
        "t2s_schema_version_info", {"version": "v2"}
    ) == pytest.approx(1.0)


class _Recorder:
    """A minimal `PipelineObserver` (structurally) that just records every
    call, for asserting fan-out order/content without depending on
    `tests.integration.test_query_cache.RecordingObserver`."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def on_stage(self, stage: str, ms: float) -> None:
        self.events.append(("stage", {"stage": stage, "ms": ms}))

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        self.events.append(("llm", {"stage": stage, "model": model}))

    def on_guard_reject(self, reason: str) -> None:
        self.events.append(("guard_reject", {"reason": reason}))

    def on_execution(self, *, outcome: str, ms: float) -> None:
        self.events.append(("execution", {"outcome": outcome}))

    def on_cache(self, *, cache: str, outcome: str) -> None:
        self.events.append(("cache", {"cache": cache, "outcome": outcome}))

    def on_pipeline_done(self, *, ms: float, ok: bool) -> None:
        self.events.append(("pipeline_done", {"ok": ok}))


def test_composite_observer_fans_every_event_out_to_every_wrapped_observer() -> None:
    metrics = Metrics()
    metrics_observer = _observer(metrics)
    recorder = _Recorder()
    composite = CompositeObserver([recorder, metrics_observer])

    composite.on_stage("retrieve", 5.0)
    composite.on_llm(stage="generate", model="gpt-4o-mini", usage=Usage(10, 5, 100.0))
    composite.on_guard_reject("not_select")
    composite.on_execution(outcome="success", ms=20.0)
    composite.on_cache(cache="sql", outcome="miss")
    composite.on_pipeline_done(ms=50.0, ok=True)

    # The recorder saw every event, in order.
    assert [kind for kind, _ in recorder.events] == [
        "stage",
        "llm",
        "guard_reject",
        "execution",
        "cache",
        "pipeline_done",
    ]

    # ... and so did `metrics` -- one sample per event.
    assert metrics.registry.get_sample_value(
        "t2s_llm_tokens_total",
        {"provider": "openai", "model": "gpt-4o-mini", "direction": "prompt"},
    ) == pytest.approx(10.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_guard_rejections_total", {"reason": "not_select"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_sql_execution_total", {"outcome": "success"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_cache_requests_total", {"cache": "sql", "outcome": "miss"}
    ) == pytest.approx(1.0)
    assert metrics.registry.get_sample_value(
        "t2s_pipeline_duration_seconds_count", {"ok": "true"}
    ) == pytest.approx(1.0)
