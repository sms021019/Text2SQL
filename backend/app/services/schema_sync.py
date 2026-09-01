"""Propagate a schema refresh from the process that ran it to every other
process sharing the Redis (API replicas, the arq worker) -- ADR 0004.

Mechanism: an opaque epoch token under `schema:epoch`. `mark_refreshed()`
bumps it after `refresh_components()`; `watch_schema_epoch()` polls it and,
on a change, runs `refresh_components()` locally. Redis down or disabled
degrades to per-process refresh only, never to an error.

Polling rather than pub/sub, deliberately: it needs no second connection
type in `RedisCache` (which is request/response only), and a process that
was restarting when the notification went out still catches up on its next
tick instead of missing the refresh forever. The cost is bounded staleness
of `Settings.schema_sync_poll_s`, plus one `GET` per process per tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.services.bootstrap import AppComponents, refresh_components

__all__ = ["log_task_exit", "mark_refreshed", "sync_schema_if_stale", "watch_schema_epoch"]

logger = logging.getLogger(__name__)


async def mark_refreshed(components: AppComponents) -> None:
    """Announce a refresh this process just ran: bump the shared epoch and
    adopt it as our own, so our own watcher does not refresh again for our
    own bump. A failed write (Redis down/disabled) is a no-op -- we keep the
    components we just rebuilt; the other processes simply never hear."""
    epoch = await components.schema_cache.bump_epoch()
    if epoch is not None:
        components.schema_epoch = epoch


async def sync_schema_if_stale(components: AppComponents) -> bool:
    """One watcher iteration: if the shared epoch differs from the one this
    process has accounted for, re-introspect and adopt it. Returns whether a
    refresh happened.

    Never raises for Redis trouble (`get_epoch` answers `None`, which reads
    as "nothing to do"), and a refresh that blows up leaves the current
    schema serving and the epoch unadopted, so the next tick retries.

    `invalidate=False`: the process that bumped the epoch stored its fresh
    `schema:{version}:*` index *before* the bump made it visible, so we load
    that index instead of deleting it and re-embedding the same summaries --
    one build plus N-1 cache hits per refresh, not N builds. See
    `refresh_components`' docstring for the full reasoning.
    """
    epoch = await components.schema_cache.get_epoch()
    if epoch is None or epoch == components.schema_epoch:
        return False
    try:
        graph = await refresh_components(components, invalidate=False)
    except Exception:
        logger.exception("schema sync: refresh failed, keeping current schema")
        return False
    components.schema_epoch = epoch
    logger.info("schema sync: refreshed", extra={"schema_version": graph.version})
    return True


async def watch_schema_epoch(
    components: AppComponents,
    *,
    period_s: float,
    on_refreshed: Callable[[], None] | None = None,
) -> None:
    """Run `sync_schema_if_stale` every `period_s` seconds forever; the owner
    of the task (`app.main`'s lifespan, `app.jobs.worker`'s startup) cancels
    it on shutdown. `on_refreshed` runs after each successful refresh -- the
    API passes a closure that re-runs `publish_components(app.state, ...)`,
    since the handlers read the `app.state` mirror rather than the
    `AppComponents` this loop mutates."""
    while True:
        if await sync_schema_if_stale(components) and on_refreshed is not None:
            on_refreshed()
        await asyncio.sleep(period_s)


def log_task_exit(name: str) -> Callable[[asyncio.Task[None]], None]:
    """A done-callback that warns if one of the endless background loops
    died on its own.

    `watch_schema_epoch` is meant to run until its owner (`app.main`'s
    lifespan, `app.jobs.worker`'s startup) cancels it, so any other exit is
    silent breakage -- a process that quietly stops noticing other
    processes' schema refreshes -- with nothing in the log to say why. It
    lives here, next to the loop it mainly guards, so that both entry points
    can attach it without the worker importing `app.main`; the lifespan's
    queue-depth sampler (a frozen `t2s_jobs_queue_depth` when it dies) uses
    it too.
    """

    def log_exit(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("%s stopped", name, exc_info=task.exception())

    return log_exit
