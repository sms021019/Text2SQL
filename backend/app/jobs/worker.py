"""`WorkerSettings`: the arq worker entry point, run as

    arq app.jobs.worker.WorkerSettings

(see the `worker` service in `docker-compose.yml`).

`on_startup` builds one `AppComponents` -- the same builder `app.main`'s
lifespan uses, so a job runs through the identical pipeline, caches, and
schema version the synchronous routes do -- and puts it in `ctx` for
`app.jobs.tasks` to read; `on_shutdown` closes it.

The worker does *not* migrate the app database: `app.main`'s lifespan owns
that, so the two processes never race Alembic (compose starts them together).
It does get a `MetricsObserver`, since `build_components` wires one up for
free when `metrics_enabled`; there is no `/metrics` endpoint here to scrape
that registry, so job-level observability comes from the structured logs --
the observer exists only to keep the worker's pipeline wiring identical to
the API's.

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
from app.observability.logging import configure_logging
from app.services.bootstrap import build_components, close_components

__all__ = ["WorkerSettings"]

_settings = get_settings()


async def startup(ctx: dict[Any, Any]) -> None:
    configure_logging(_settings.log_level)
    ctx["components"] = await build_components(_settings)


async def shutdown(ctx: dict[Any, Any]) -> None:
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
