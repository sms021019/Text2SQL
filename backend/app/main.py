"""FastAPI application factory and the module-level `app` uvicorn serves.

`create_app()`'s lifespan migrates the app DB, builds the whole request-time
runtime via `app.services.bootstrap.build_components()` (shared verbatim with
the arq worker, so an async job answers exactly as the synchronous route
does) and publishes it onto `app.state` for the handlers in `app/api/v1/**`
to read via `app/api/deps.py`.

On top of that runtime the lifespan owns two queue-side concerns the worker
has no use for: the `ArqRedis` pool `app/api/v1/jobs.py` enqueues through,
and a background sampler that reads the queue's depth into the
`t2s_jobs_queue_depth` gauge. Both degrade quietly when Redis is
unreachable -- no pool means `POST /query/async` answers 503 while
`POST /query` keeps working.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app
from prometheus_fastapi_instrumentator import Instrumentator
from redis.exceptions import RedisError

from app.api.router import router
from app.cache.redis import RedisCache
from app.config import Settings, get_settings
from app.core.observer import PipelineObserver
from app.db.migrate import upgrade_to_head
from app.llm.base import LLMClient
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.observability.middleware import RequestIDMiddleware
from app.services.bootstrap import build_components, close_components, publish_components

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)


async def _create_queue_pool(settings: Settings) -> ArqRedis | None:
    """Open the `ArqRedis` pool `POST /api/v1/query/async` enqueues through,
    or `None` if Redis is unreachable -- a queue outage degrades the async
    endpoints to 503 rather than failing startup.

    `conn_retries=0` overrides arq's default of five one-second retries: a
    dead Redis must not add six seconds to every start (and the container
    `HEALTHCHECK`'s `--start-period` budget).
    """
    redis_settings = replace(RedisSettings.from_dsn(settings.redis_url), conn_retries=0)
    try:
        return await create_pool(redis_settings)
    except (RedisError, OSError) as exc:
        logger.warning("job queue unavailable, POST /api/v1/query/async will 503: %s", exc)
        return None


async def _sample_queue_depth(pool: ArqRedis, metrics: Metrics, period_s: float) -> None:
    """Publish the arq queue's depth into `t2s_jobs_queue_depth` every
    `period_s` seconds, forever (the lifespan cancels this task on
    shutdown).

    A Redis blip skips the sample and leaves the gauge on its last reading
    rather than raising or logging -- this loop runs every few seconds, so
    logging each failure would flood the log for as long as the outage lasts,
    and a stale gauge is the signal.
    """
    while True:
        try:
            depth = await pool.zcard(default_queue_name)
        except (RedisError, OSError):
            pass
        else:
            metrics.jobs_queue_depth.set(depth)
        await asyncio.sleep(period_s)


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
    resolve. An injected `llm`/`redis` stays the caller's to close -- see
    `app.services.bootstrap`'s Ownership notes."""
    resolved_settings = settings or get_settings()

    # Built here rather than inside the lifespan because `/metrics` is
    # mounted against this registry below, before the lifespan ever runs.
    metrics = Metrics()

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

        components = await build_components(
            resolved_settings, llm=llm, redis=redis, observer=observer, metrics=metrics
        )
        publish_components(app.state, components)

        arq_pool = await _create_queue_pool(resolved_settings)
        app.state.arq_pool = arq_pool

        sampler: asyncio.Task[None] | None = None
        if arq_pool is not None and resolved_settings.metrics_enabled:
            sampler = asyncio.create_task(
                _sample_queue_depth(arq_pool, metrics, resolved_settings.queue_depth_sample_s)
            )

        app.state.ready = True

        try:
            yield
        finally:
            app.state.ready = False
            if sampler is not None:
                sampler.cancel()
                with suppress(asyncio.CancelledError):
                    await sampler
            if arq_pool is not None:
                await arq_pool.aclose()
                app.state.arq_pool = None
            await close_components(components)

    app = FastAPI(title="Text2SQL API", lifespan=lifespan)
    app.state.ready = False
    app.state.arq_pool = None

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
