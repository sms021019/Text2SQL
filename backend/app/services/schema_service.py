"""`prepare_retriever()`: build a `SchemaRetriever` for a freshly-introspected
`SchemaGraph`, preferring a cached embedding index over calling the LLM.
`build_pipeline()`: build the matching `QueryCache` + `Text2SQLPipeline`
pair for a given schema graph/retriever.

Shared by `app.main`'s lifespan (cold start) and `POST /api/v1/schema/refresh`
(`app/api/v1/schema.py`) so the "load from cache, else bounded-embed-then-store"
flow -- including the startup-embed-timeout tolerance `app.main` already had
before this helper existed -- lives in exactly one place. Likewise,
`build_pipeline()` exists so both call sites construct the query cache (which
bakes in `graph.version` at construction time -- see
`app.cache.query_cache.QueryCache`) and the pipeline pointed at it the same
way; a schema refresh that only swapped `state.graph` without rebuilding both
would keep serving cache entries keyed to the *old* schema version forever.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from app.cache.query_cache import QueryCache
from app.cache.redis import RedisCache
from app.cache.schema_cache import SchemaCache
from app.config import Settings
from app.core.observer import NullObserver, PipelineObserver
from app.core.pipeline import Text2SQLPipeline
from app.core.prompting.builder import PromptBuilder
from app.core.schema.models import SchemaGraph
from app.core.schema.retrieve import SchemaRetriever
from app.llm.base import LLMClient

__all__ = ["build_pipeline", "prepare_retriever", "SchemaIndexSource"]

logger = logging.getLogger(__name__)

#: Where a `SchemaRetriever`'s built index came from -- `"cache"` (a
#: `SchemaCache.load()` hit), `"embedded"` (built via the LLM this call), or
#: `"failed"` (neither succeeded; the retriever self-heals lazily on first
#: use, per `SchemaRetriever.mark_build_failed()`'s docstring). Recorded on
#: `app.state.schema_index_source` for observability; also reported to
#: `observer` as a `cache="schema"` event below (`"cache"` -> `"hit"`,
#: `"embedded"`/`"failed"` -> `"miss"`) -- not part of any HTTP response.
SchemaIndexSource = Literal["cache", "embedded", "failed"]

#: `SchemaIndexSource` -> the `outcome` reported to
#: `observer.on_cache(cache="schema", outcome=...)` below.
_CACHE_OUTCOME: dict[SchemaIndexSource, str] = {
    "cache": "hit",
    "embedded": "miss",
    "failed": "miss",
}


async def prepare_retriever(
    graph: SchemaGraph,
    llm: LLMClient,
    settings: Settings,
    schema_cache: SchemaCache,
    observer: PipelineObserver | None = None,
    *,
    llm_observer: PipelineObserver | None = None,
) -> tuple[SchemaRetriever, SchemaIndexSource]:
    """Return a `SchemaRetriever` for `graph`, built either from a cached
    index (`SchemaCache.load(graph.version)`, no LLM call) or, on a miss, by
    embedding via `SchemaRetriever.build_index()` -- bounded by
    `settings.startup_embed_timeout_s` exactly as `app.main`'s lifespan
    bounded it before this helper existed, tolerating a slow/unreachable LLM
    via `mark_build_failed()` rather than raising. A freshly built index is
    stored back into `schema_cache` for next time; a cache hit is not
    re-stored.

    `observer` (defaulting to a no-op `NullObserver`) gets exactly one
    `on_cache(cache="schema", outcome=...)` call per invocation, mapped from
    the returned `SchemaIndexSource` -- see `_CACHE_OUTCOME`. Callers pass a
    dedicated metrics-only observer here (`app.services.bootstrap`'s
    `schema_observer`), not the per-request pipeline observer a test might
    inject, since this runs once at startup/refresh rather than per query.
    On a cache *miss* that same `observer` additionally gets the index
    build's `on_llm(stage="embed", ...)` event, for the same reason --
    passed to `build_index()` explicitly; a hit calls the LLM not at all,
    so it reports nothing beyond the one `on_cache`.

    `llm_observer` is the observer the returned retriever keeps for the rest
    of its life: every *request-time* question embedding is reported there,
    so it belongs on the pipeline observer chain (metrics plus any injected
    observer) alongside `generate`/`repair`.
    """
    resolved_observer: PipelineObserver = observer if observer is not None else NullObserver()
    retriever = SchemaRetriever(graph, llm, top_k=settings.retrieve_top_k, observer=llm_observer)

    cached = await schema_cache.load(graph.version)
    if cached is not None:
        _cached_graph, names, matrix = cached
        try:
            retriever.import_index(names, matrix)
        except ValueError as exc:
            # Defensive only: schema_cache.load() already checked the
            # version, and store() always writes names/matrix that agree --
            # a mismatch here means a foreign/corrupted entry. Fall through
            # to a normal (re-)build rather than raising.
            logger.warning("schema cache entry invalid, rebuilding: %s", exc)
        else:
            logger.info("schema_cache_hit version=%s", graph.version)
            resolved_observer.on_cache(cache="schema", outcome=_CACHE_OUTCOME["cache"])
            return retriever, "cache"

    try:
        await asyncio.wait_for(
            retriever.build_index(observer=resolved_observer),
            timeout=settings.startup_embed_timeout_s,
        )
    except TimeoutError:
        logger.warning(
            "schema index build timed out after %ss, will retry lazily",
            settings.startup_embed_timeout_s,
        )
        retriever.mark_build_failed(
            f"embedding timed out during startup after {settings.startup_embed_timeout_s}s"
        )

    exported = retriever.export_index()
    if exported is None:
        resolved_observer.on_cache(cache="schema", outcome=_CACHE_OUTCOME["failed"])
        return retriever, "failed"

    names, matrix = exported
    await schema_cache.store(graph, names, matrix)
    logger.info("schema_cache_store version=%s", graph.version)
    resolved_observer.on_cache(cache="schema", outcome=_CACHE_OUTCOME["embedded"])
    return retriever, "embedded"


def build_pipeline(
    *,
    llm: LLMClient,
    retriever: SchemaRetriever,
    graph: SchemaGraph,
    builder: PromptBuilder,
    target_engine: AsyncEngine,
    settings: Settings,
    redis: RedisCache,
    observer: PipelineObserver | None = None,
) -> tuple[Text2SQLPipeline, QueryCache]:
    """Build the `QueryCache` for `graph.version` and the `Text2SQLPipeline`
    wired to use it, together -- see the module docstring for why these two
    must always be constructed as a pair.

    With `settings.cache_enabled` false the pipeline is handed `cache=None`
    rather than a cache whose every read is a no-op: that is the only way
    `PipelineOutput.cache_status` reports `"disabled"` (a disabled
    `RedisCache` just returns `None`, which the pipeline cannot tell from a
    genuine miss, so every request would be counted as a miss in
    `t2s_cache_requests_total`). The `QueryCache` is still built and
    returned -- it is what `app.state.query_cache` mirrors, and it costs
    nothing beyond the key derivation it would do anyway.
    """
    query_cache = QueryCache(
        redis,
        schema_version=graph.version,
        model=settings.llm_model,
        prompt_version=settings.prompt_version,
        sql_ttl_s=settings.sql_cache_ttl_s,
        result_ttl_s=settings.result_cache_ttl_s,
        result_max_bytes=settings.result_cache_max_bytes,
    )
    pipeline = Text2SQLPipeline(
        llm=llm,
        retriever=retriever,
        graph=graph,
        builder=builder,
        target_engine=target_engine,
        settings=settings,
        observer=observer,
        cache=query_cache if settings.cache_enabled else None,
    )
    return pipeline, query_cache
