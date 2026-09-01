"""`AppComponents` + `build_components()`/`close_components()`: the one
place that turns a `Settings` into a fully wired Text2SQL runtime (both
database engines, the LLM client, the Redis caches, the schema graph and
retriever, the prompt builder, the pipeline, and the metrics observers).

Extracted out of `app.main`'s lifespan so the FastAPI app and the arq worker
(`app.jobs.worker.WorkerSettings.on_startup`) build *identical* runtimes
instead of two copies drifting apart -- a job answering a question must go
through exactly the same pipeline, cache, and schema version the synchronous
route does. `publish_components()` mirrors a built `AppComponents` onto
`app.state` for `app/api/deps.py` to read; `refresh_components()` re-does the
introspect/retrieve/pipeline half of the build in place, shared by
`POST /api/v1/schema/refresh` and `app.jobs.tasks.refresh_schema_job`
(whichever of those ran it then announces it to the other processes via
`app.services.schema_sync`).

Ownership
---------
`close_components()` closes exactly what `build_components()` *built*, never
what a caller injected: both engines are always built here, so both are
always disposed; the `llm` and `redis` are disposed only when they were not
passed in (`AppComponents.owns_llm`/`owns_redis`). An injected `llm`,
`redis`, or `observer` belongs to whoever created it -- typically a test,
which closes it itself -- and outliving one `build_components()` call is the
whole point of injecting it. Migrating the app database and configuring
logging are deliberately *not* done here: they are process-level concerns
owned by the entry point (`app.main`'s lifespan migrates; the worker does
not, so two processes never race Alembic).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.datastructures import State

from app.cache.query_cache import QueryCache
from app.cache.redis import RedisCache
from app.cache.schema_cache import SchemaCache
from app.config import Settings
from app.core.observer import NullObserver, PipelineObserver
from app.core.pipeline import Text2SQLPipeline
from app.core.prompting.builder import PromptBuilder
from app.core.schema.introspect import introspect
from app.core.schema.models import SchemaGraph
from app.core.schema.retrieve import SchemaRetriever
from app.db.session import make_engine
from app.llm.base import LLMClient
from app.llm.factory import build_llm
from app.observability.metrics import (
    CompositeObserver,
    Metrics,
    MetricsObserver,
    set_schema_version,
)
from app.services.schema_service import SchemaIndexSource, build_pipeline, prepare_retriever

__all__ = [
    "AppComponents",
    "build_components",
    "close_components",
    "publish_components",
    "refresh_components",
]

logger = logging.getLogger(__name__)


@dataclass
class AppComponents:
    """Everything one Text2SQL runtime needs, built together by
    `build_components()`.

    Mutable on purpose: `refresh_components()` swaps `graph`/`retriever`/
    `pipeline`/`query_cache`/`schema_index_source` in place when the target
    schema is re-introspected.
    """

    settings: Settings
    target_engine: AsyncEngine
    app_engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    llm: LLMClient
    redis: RedisCache
    schema_cache: SchemaCache
    query_cache: QueryCache
    graph: SchemaGraph
    retriever: SchemaRetriever
    builder: PromptBuilder
    pipeline: Text2SQLPipeline
    metrics: Metrics
    #: The observer handed to `Text2SQLPipeline` -- a `MetricsObserver`, an
    #: injected observer, a `CompositeObserver` of both, or a `NullObserver`.
    observer: PipelineObserver
    #: Metrics-only observer for what `prepare_retriever` reports at
    #: startup/refresh: the `cache="schema"` event always, plus the index
    #: build's `stage="embed"` event on a miss -- see `_resolve_observers`.
    schema_observer: PipelineObserver
    schema_index_source: SchemaIndexSource
    #: The shared `schema:epoch` token this process has already accounted
    #: for -- `None` when Redis never had one (or is down/disabled). Set at
    #: build time so a process starting *after* someone else's refresh does
    #: not immediately refresh again for it; advanced by
    #: `app.services.schema_sync` on both sides of the propagation.
    schema_epoch: str | None
    owns_llm: bool
    owns_redis: bool


def _load_examples(path: str) -> list[dict[str, Any]]:
    """Load few-shot examples for `PromptBuilder` from `path`.

    Tolerates a missing file (returns no examples, with a warning) -- see
    `Settings.examples_path`'s docstring for why the file may not be there.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("examples file not found, continuing with no examples: %s", p)
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not data:
        return []
    return list(data)


def _resolve_observers(
    settings: Settings, metrics: Metrics, observer: PipelineObserver | None
) -> tuple[PipelineObserver, PipelineObserver]:
    """Return `(pipeline_observer, schema_observer)`.

    The schema observer is deliberately *not* the pipeline observer: it only
    ever sees what `prepare_retriever` reports at startup/refresh -- the one
    `on_cache(cache="schema", ...)` call and, on a cache miss, the index
    build's `on_llm(stage="embed", ...)` -- kept off of any caller-injected
    `observer` (e.g. a test's `RecordingObserver`) so that observer's event
    stream stays exactly one request's worth of pipeline events, nothing
    from startup. Metrics still see both, via the `MetricsObserver` directly.

    The pipeline observer is handed to `prepare_retriever` separately, as
    `llm_observer`: the retriever keeps it for the *request-time* question
    embedding, which belongs with `generate`/`repair` on the request's own
    observer chain.
    """
    if not settings.metrics_enabled:
        return (observer if observer is not None else NullObserver()), NullObserver()

    metrics_observer = MetricsObserver(
        metrics, provider=settings.llm_provider, prices=settings.llm_prices_usd_per_1k
    )
    pipeline_observer: PipelineObserver = (
        CompositeObserver([observer, metrics_observer])
        if observer is not None
        else metrics_observer
    )
    return pipeline_observer, metrics_observer


async def build_components(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    redis: RedisCache | None = None,
    observer: PipelineObserver | None = None,
    metrics: Metrics | None = None,
) -> AppComponents:
    """Build a complete runtime from `settings`.

    `llm`/`redis`/`observer` are injectable so tests (and `create_app`'s own
    injection points) can substitute a `FakeLLM`, a specific `RedisCache`, or
    a recording observer; injected objects are never closed by
    `close_components` -- see the module docstring. `metrics` is injectable
    because `create_app` needs the `CollectorRegistry` *before* the lifespan
    runs, to mount `/metrics` against it.
    """
    resolved_metrics = metrics if metrics is not None else Metrics()
    pipeline_observer, schema_observer = _resolve_observers(settings, resolved_metrics, observer)

    target_engine = make_engine(settings.target_db_url)
    app_engine = make_engine(settings.app_db_url)
    session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

    resolved_llm = llm if llm is not None else build_llm(settings)
    resolved_redis = (
        redis
        if redis is not None
        else RedisCache(settings.redis_url, enabled=settings.cache_enabled)
    )
    schema_cache = SchemaCache(resolved_redis, ttl_s=settings.schema_cache_ttl_s)

    graph = await introspect(target_engine)
    retriever, schema_index_source = await prepare_retriever(
        graph,
        resolved_llm,
        settings,
        schema_cache,
        observer=schema_observer,
        llm_observer=pipeline_observer,
    )
    if settings.metrics_enabled:
        set_schema_version(resolved_metrics, graph.version)

    builder = PromptBuilder(settings.prompt_version, _load_examples(settings.examples_path))

    pipeline, query_cache = build_pipeline(
        llm=resolved_llm,
        retriever=retriever,
        graph=graph,
        builder=builder,
        target_engine=target_engine,
        settings=settings,
        redis=resolved_redis,
        observer=pipeline_observer,
    )

    # Baseline, not a bump: adopting whatever epoch is already in Redis
    # means this process starts in agreement with everyone else and only
    # refreshes for a bump that happens from now on.
    schema_epoch = await schema_cache.get_epoch()

    return AppComponents(
        settings=settings,
        target_engine=target_engine,
        app_engine=app_engine,
        session_factory=session_factory,
        llm=resolved_llm,
        redis=resolved_redis,
        schema_cache=schema_cache,
        query_cache=query_cache,
        graph=graph,
        retriever=retriever,
        builder=builder,
        pipeline=pipeline,
        metrics=resolved_metrics,
        observer=pipeline_observer,
        schema_observer=schema_observer,
        schema_index_source=schema_index_source,
        schema_epoch=schema_epoch,
        owns_llm=llm is None,
        owns_redis=redis is None,
    )


async def close_components(components: AppComponents) -> None:
    """Release everything `build_components()` built -- and only that; see
    the module docstring's Ownership section."""
    await components.target_engine.dispose()
    await components.app_engine.dispose()
    if components.owns_redis:
        await components.redis.aclose()
    if components.owns_llm:
        aclose = getattr(components.llm, "aclose", None)
        if aclose is not None:
            await aclose()


async def refresh_components(components: AppComponents, *, invalidate: bool = True) -> SchemaGraph:
    """Re-introspect the target database and swap the schema-dependent half
    of `components` (graph, retriever, pipeline, query cache) in place,
    returning the new `SchemaGraph`.

    Two callers, two modes:

    * `invalidate=True` (the default) is the *refresh* path -- `POST
      /api/v1/schema/refresh` and `app.jobs.tasks.refresh_schema_job`.
      Dropping the shared cache first is what makes an explicit refresh mean
      something for an unchanged schema: re-introspection then yields the
      same `graph.version`, so without the sweep `prepare_retriever` would
      hit the cache and keep serving the very index the operator asked to
      rebuild.
    * `invalidate=False` is the *propagation* path --
      `app.services.schema_sync.sync_schema_if_stale`. The process that ran
      the refresh already stored a fresh `schema:{version}:*` index before
      its `mark_refreshed` made the new epoch visible, so a follower must
      *load* that index rather than delete it. Invalidating here would cost
      a full re-embed in every follower instead of one build plus N-1 cache
      hits, could leave a follower whose LLM is down with a
      `mark_build_failed` retriever, and would run `invalidate_all`'s epoch
      read-modify-write concurrently with the next refresher's bump.

    Deliberately lock-free in both modes: two refreshes that overlap each
    rebuild in full and each swap atomically, so the loser's work is wasted
    but never half-applied.
    """
    settings = components.settings

    if invalidate:
        await components.schema_cache.invalidate_all()

    graph = await introspect(components.target_engine)
    retriever, schema_index_source = await prepare_retriever(
        graph,
        components.llm,
        settings,
        components.schema_cache,
        observer=components.schema_observer,
        llm_observer=components.observer,
    )
    if settings.metrics_enabled:
        set_schema_version(components.metrics, graph.version)
    # `build_pipeline` bakes `graph.version` into the new `QueryCache`, so a
    # refresh that changed the schema (even to a version with the same
    # tables re-hashed differently) starts every SQL/result-cache lookup
    # fresh rather than serving entries keyed to the old version.
    pipeline, query_cache = build_pipeline(
        llm=components.llm,
        retriever=retriever,
        graph=graph,
        builder=components.builder,
        target_engine=components.target_engine,
        settings=settings,
        redis=components.redis,
        observer=components.observer,
    )

    # No `await` between here and the assignments above, so this swap is
    # atomic with respect to any other task on the same event loop.
    components.graph = graph
    components.retriever = retriever
    components.pipeline = pipeline
    components.query_cache = query_cache
    components.schema_index_source = schema_index_source
    return graph


def publish_components(state: State, components: AppComponents) -> None:
    """Mirror `components` onto a Starlette `app.state` (`state`), where
    `app/api/deps.py` and the route handlers read them from. Called once by
    `app.main`'s lifespan and again by `POST /api/v1/schema/refresh` after
    `refresh_components()` has swapped the schema-dependent half."""
    state.components = components
    state.settings = components.settings
    state.target_engine = components.target_engine
    state.app_engine = components.app_engine
    state.session_factory = components.session_factory
    state.llm = components.llm
    state.redis = components.redis
    state.schema_cache = components.schema_cache
    state.query_cache = components.query_cache
    state.graph = components.graph
    state.retriever = components.retriever
    state.builder = components.builder
    state.pipeline = components.pipeline
    state.metrics = components.metrics
    state.observer = components.observer
    state.schema_observer = components.schema_observer
    state.schema_index_source = components.schema_index_source
