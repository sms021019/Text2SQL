"""Unit tests for `app.services.schema_service.prepare_retriever`'s observer
wiring: which of its two observers each embedding call is reported to.

The split matters and is easy to break silently -- see
`app.services.bootstrap._resolve_observers`. The startup/refresh index build
must reach only the metrics-only schema observer, so a caller-injected
observer (e.g. a test's recording observer) keeps seeing exactly one
request's worth of events; the per-question embedding the retriever makes
later must reach the pipeline observer instead, alongside `generate` and
`repair`.

No container needed: a disabled `RedisCache` reads as a guaranteed cache
miss (so the index really is built) and swallows the store afterwards.
"""

from __future__ import annotations

from app.cache.redis import RedisCache
from app.cache.schema_cache import SchemaCache
from app.config import Settings
from app.core.observer import NullObserver
from app.core.schema.models import Column, SchemaGraph, Table
from app.llm.base import Usage
from app.services.schema_service import prepare_retriever
from tests.fakes.llm import FakeLLM


class RecordingObserver(NullObserver):
    def __init__(self) -> None:
        self.llm_calls: list[tuple[str, str, Usage]] = []

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        self.llm_calls.append((stage, model, usage))


def _graph() -> SchemaGraph:
    orders = Table(
        name="orders",
        comment="Customer orders.",
        columns=[Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True)],
        foreign_keys=[],
    )
    return SchemaGraph.build(tables={"orders": orders})


def _schema_cache() -> SchemaCache:
    # `enabled=False` never touches the network: every read is a miss and
    # every write a no-op, which is exactly the cold-cache path here.
    return SchemaCache(RedisCache("redis://localhost:1", enabled=False), ttl_s=60)


async def test_index_build_and_question_embeds_go_to_different_observers() -> None:
    schema_observer = RecordingObserver()
    pipeline_observer = RecordingObserver()
    settings = Settings(_env_file=None)

    retriever, source = await prepare_retriever(
        _graph(),
        FakeLLM(),
        settings,
        _schema_cache(),
        observer=schema_observer,
        llm_observer=pipeline_observer,
    )
    assert source == "embedded"  # cold cache: the index really was built

    # The build is startup work: only the schema observer hears about it.
    assert [stage for stage, _, _ in schema_observer.llm_calls] == ["embed"]
    assert pipeline_observer.llm_calls == []

    await retriever.retrieve("how many orders?")

    # The question embedding is request work: only the pipeline observer.
    assert [stage for stage, _, _ in pipeline_observer.llm_calls] == ["embed"]
    assert [stage for stage, _, _ in schema_observer.llm_calls] == ["embed"]
    assert pipeline_observer.llm_calls[0][1] == "fake-embed"
    assert pipeline_observer.llm_calls[0][2].prompt_tokens == 1


async def test_prepare_retriever_without_an_llm_observer_reports_no_request_events() -> None:
    """`llm_observer` is optional: omitting it leaves the retriever with a
    `NullObserver`, so a question embed reports nowhere -- and in particular
    never leaks onto the schema observer."""
    schema_observer = RecordingObserver()

    retriever, _source = await prepare_retriever(
        _graph(),
        FakeLLM(),
        Settings(_env_file=None),
        _schema_cache(),
        observer=schema_observer,
    )
    await retriever.retrieve("how many orders?")

    assert [stage for stage, _, _ in schema_observer.llm_calls] == ["embed"]
