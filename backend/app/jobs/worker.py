"""`WorkerSettings`: the arq worker entry point, run as

    arq app.jobs.worker.WorkerSettings

(see the `worker` service in `docker-compose.yml`).

`on_startup` builds one `AppComponents` -- the same builder `app.main`'s
lifespan uses, so a job runs through the identical pipeline, caches, and
schema version the synchronous routes do -- and puts it in `ctx` for
`app.jobs.tasks` to read; `on_shutdown` closes it.

The worker does *not* migrate the app database: `app.main`'s lifespan owns
that, so the two processes never race Alembic (compose orders the worker
after a healthy `backend`, so the migration is done before the first job).

`build_components` wires up a `MetricsObserver` for free when
`metrics_enabled`, and `on_startup` now serves that registry itself:
`app.observability.exporter.start_metrics_server` puts the standard
Prometheus exposition on `http://0.0.0.0:$WORKER_METRICS_PORT/metrics`
(default `9100`, `0` disables it) from a daemon thread, since there is no
ASGI app here to hang a route off. Prometheus scrapes it as the
`text2sql-worker` job, so job-level `t2s_*` series are graphable rather than
log-only -- see ADR 0005. `t2s_jobs_queue_depth` stays API-side: one
process samples the queue, and the worker reading the same `ZCARD` would
just publish a duplicate series under a second `job` label.

Tests do not use `WorkerSettings`: they build their own
`arq.worker.Worker(..., ctx={"components": ...})` with a `FakeLLM`, since
arq seeds `Worker(ctx=...)` into the job context directly -- no override hook
needed here. See `tests/integration/test_jobs.py`.
"""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.jobs.tasks import refresh_schema_job, run_query_job
from app.observability.exporter import start_metrics_server
from app.observability.logging import configure_logging
from app.services.bootstrap import build_components, close_components

__all__ = ["WorkerSettings"]

_settings = get_settings()


async def startup(ctx: dict[Any, Any]) -> None:
    configure_logging(_settings.log_level)
    components = await build_components(_settings)
    ctx["components"] = components
    if _settings.metrics_enabled and _settings.worker_metrics_port > 0:
        ctx["metrics_server"] = start_metrics_server(
            components.metrics.registry, port=_settings.worker_metrics_port
        )


async def shutdown(ctx: dict[Any, Any]) -> None:
    server = ctx.pop("metrics_server", None)
    if server is not None:
        server.close()
    components = ctx.pop("components", None)
    if components is not None:
        await close_components(components)


class WorkerSettings:
    functions = [run_query_job, refresh_schema_job]
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = _settings.arq_max_jobs
    job_timeout = 300
    # Plain assignment, not `staticmethod`: arq reads these straight out of
    # `WorkerSettings.__dict__` (`arq.worker.get_kwargs`), bypassing the
    # descriptor protocol, so it must find the coroutine functions there.
    on_startup = startup
    on_shutdown = shutdown
