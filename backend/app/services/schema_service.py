"""`prepare_retriever()`: build a `SchemaRetriever` for a freshly-introspected
`SchemaGraph`, preferring a cached embedding index over calling the LLM.

Shared by `app.main`'s lifespan (cold start) and `POST /api/v1/schema/refresh`
(`app/api/v1/schema.py`) so the "load from cache, else bounded-embed-then-store"
flow -- including the startup-embed-timeout tolerance `app.main` already had
before this helper existed -- lives in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app.cache.schema_cache import SchemaCache
from app.config import Settings
from app.core.schema.models import SchemaGraph
from app.core.schema.retrieve import SchemaRetriever
from app.llm.base import LLMClient

__all__ = ["prepare_retriever", "SchemaIndexSource"]

logger = logging.getLogger(__name__)

#: Where a `SchemaRetriever`'s built index came from -- `"cache"` (a
#: `SchemaCache.load()` hit), `"embedded"` (built via the LLM this call), or
#: `"failed"` (neither succeeded; the retriever self-heals lazily on first
#: use, per `SchemaRetriever.mark_build_failed()`'s docstring). Recorded on
#: `app.state.schema_index_source` for observability (Phase 2 Task 4 wires it
#: into metrics/logs); not part of any HTTP response.
SchemaIndexSource = Literal["cache", "embedded", "failed"]


async def prepare_retriever(
    graph: SchemaGraph,
    llm: LLMClient,
    settings: Settings,
    schema_cache: SchemaCache,
) -> tuple[SchemaRetriever, SchemaIndexSource]:
    """Return a `SchemaRetriever` for `graph`, built either from a cached
    index (`SchemaCache.load(graph.version)`, no LLM call) or, on a miss, by
    embedding via `SchemaRetriever.build_index()` -- bounded by
    `settings.startup_embed_timeout_s` exactly as `app.main`'s lifespan
    bounded it before this helper existed, tolerating a slow/unreachable LLM
    via `mark_build_failed()` rather than raising. A freshly built index is
    stored back into `schema_cache` for next time; a cache hit is not
    re-stored.
    """
    retriever = SchemaRetriever(graph, llm, top_k=settings.retrieve_top_k)

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
            return retriever, "cache"

    try:
        await asyncio.wait_for(retriever.build_index(), timeout=settings.startup_embed_timeout_s)
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
        return retriever, "failed"

    names, matrix = exported
    await schema_cache.store(graph, names, matrix)
    logger.info("schema_cache_store version=%s", graph.version)
    return retriever, "embedded"
