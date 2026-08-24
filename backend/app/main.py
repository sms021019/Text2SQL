"""FastAPI application factory and the module-level `app` uvicorn serves.

`create_app()` wires up the whole request-time stack in its lifespan:
migrate the app DB, open both engines, build (or accept an injected) LLM
client, introspect the target schema, build the retrieval index, and
assemble a `Text2SQLPipeline` -- all stored on `app.state` for the route
handlers in `app/api/v1/**` to read via `app/api/deps.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.router import router
from app.cache.redis import RedisCache
from app.cache.schema_cache import SchemaCache
from app.config import Settings, get_settings
from app.core.observer import NullObserver, PipelineObserver
from app.core.prompting.builder import PromptBuilder
from app.core.schema.introspect import introspect
from app.db.migrate import upgrade_to_head
from app.db.session import make_engine
from app.llm.base import LLMClient
from app.llm.factory import build_llm
from app.observability.logging import configure_logging
from app.observability.metrics import (
    CompositeObserver,
    Metrics,
    MetricsObserver,
    set_schema_version,
)
from app.observability.middleware import RequestIDMiddleware
from app.services.schema_service import build_pipeline, prepare_retriever

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)


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


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    redis: RedisCache | None = None,
    observer: PipelineObserver | None = None,
) -> FastAPI:
    """Build the FastAPI app. `settings`/`llm`/`redis`/`observer` are
    injectable so tests can point at a container database, a `FakeLLM`, a
    specific `RedisCache`, and a recording `PipelineObserver` instead of the
    real services `get_settings()`/`build_llm()` (and a `RedisCache` built
    from `settings.redis_url`, and a no-op `NullObserver`) would otherwise
    resolve."""
    resolved_settings = settings or get_settings()

    metrics = Metrics()
    if resolved_settings.metrics_enabled:
        metrics_observer = MetricsObserver(
            metrics,
            provider=resolved_settings.llm_provider,
            prices=resolved_settings.llm_prices_usd_per_1k,
        )
        # `schema_observer` is deliberately *not* `resolved_observer` below:
        # it only ever sees the one `on_cache(cache="schema", ...)` call
        # `prepare_retriever` makes at startup/refresh, kept off of any
        # caller-injected `observer` (e.g. a test's `RecordingObserver`) so
        # that observer's event stream stays exactly what it was before this
        # task -- one request's worth of pipeline events, nothing from
        # startup. Metrics still see it, via `metrics_observer` directly.
        schema_observer: PipelineObserver = metrics_observer
        resolved_observer: PipelineObserver = (
            CompositeObserver([observer, metrics_observer])
            if observer is not None
            else metrics_observer
        )
    else:
        schema_observer = NullObserver()
        resolved_observer = observer if observer is not None else NullObserver()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Configured *after* the migration, not before: Alembic's env.py
        # calls `logging.config.fileConfig(alembic.ini)`, which replaces the
        # root logger's handlers with alembic.ini's plain-text ones -- an
        # earlier `configure_logging()` call here would just get clobbered
        # by that, silently reverting every log line for the rest of the
        # process back out of JSON.
        await asyncio.to_thread(upgrade_to_head, resolved_settings.app_db_url)

        configure_logging(resolved_settings.log_level)

        target_engine = make_engine(resolved_settings.target_db_url)
        app_engine = make_engine(resolved_settings.app_db_url)
        session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

        app_llm = llm or build_llm(resolved_settings)

        redis_cache = redis or RedisCache(
            resolved_settings.redis_url, enabled=resolved_settings.cache_enabled
        )
        schema_cache = SchemaCache(redis_cache, ttl_s=resolved_settings.schema_cache_ttl_s)

        graph = await introspect(target_engine)
        retriever, schema_index_source = await prepare_retriever(
            graph, app_llm, resolved_settings, schema_cache, observer=schema_observer
        )
        if resolved_settings.metrics_enabled:
            set_schema_version(metrics, graph.version)

        examples = _load_examples(resolved_settings.examples_path)
        builder = PromptBuilder(resolved_settings.prompt_version, examples)

        pipeline, query_cache = build_pipeline(
            llm=app_llm,
            retriever=retriever,
            graph=graph,
            builder=builder,
            target_engine=target_engine,
            settings=resolved_settings,
            redis=redis_cache,
            observer=resolved_observer,
        )

        app.state.settings = resolved_settings
        app.state.target_engine = target_engine
        app.state.app_engine = app_engine
        app.state.session_factory = session_factory
        app.state.llm = app_llm
        app.state.redis = redis_cache
        app.state.schema_cache = schema_cache
        app.state.schema_index_source = schema_index_source
        app.state.graph = graph
        app.state.retriever = retriever
        app.state.builder = builder
        app.state.observer = resolved_observer
        app.state.metrics = metrics
        app.state.schema_observer = schema_observer
        app.state.query_cache = query_cache
        app.state.pipeline = pipeline
        app.state.ready = True

        try:
            yield
        finally:
            app.state.ready = False
            await target_engine.dispose()
            await app_engine.dispose()
            await redis_cache.aclose()
            aclose = getattr(app_llm, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="Text2SQL API", lifespan=lifespan)
    app.state.ready = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIDMiddleware)

    app.include_router(router)

    if resolved_settings.metrics_enabled:
        # `.instrument(app)` only (no `.expose()`): we mount the ASGI app
        # ourselves below, against `metrics.registry` rather than
        # `prometheus_client`'s process-global default registry, so tests
        # constructing multiple apps in the same process don't collide.
        Instrumentator(
            registry=metrics.registry,
            excluded_handlers=["/metrics", "/healthz", "/readyz"],
        ).instrument(app)
        app.mount("/metrics", make_asgi_app(registry=metrics.registry))

    return app


app = create_app()
