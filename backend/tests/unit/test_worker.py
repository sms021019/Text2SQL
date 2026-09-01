"""`app.jobs.worker`'s `on_startup`/`on_shutdown` wiring for the two
process-level concerns it owns beyond building the components:

- the worker's own Prometheus exposition -- the arq worker has no ASGI app,
  so `startup` is the only place that can put `AppComponents.metrics.registry`
  on the wire, and `shutdown` is the only place that can take it back off;
- the schema-epoch watcher (`app.services.schema_sync`), which must be
  started under `schema_sync_poll_s > 0` and, crucially, cancelled *and
  awaited* before `close_components` disposes what it polls.

Wiring only -- no arq, no Redis, no containers. `build_components` and
`close_components` are monkeypatched out, so what is under test is exactly
the `metrics_enabled and worker_metrics_port > 0` decision, the
`schema_sync_poll_s > 0` decision, and their paired teardown.

`worker.py` reads `_settings = get_settings()` at import time, so these
patch `app.jobs.worker._settings` with a fresh `Settings(_env_file=None,
...)` rather than the environment -- an env var would be too late.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.observability.metrics import Metrics


def _is_watcher(task: asyncio.Task[Any]) -> bool:
    """Whether `task` is running `watch_schema_epoch`. Reads the coroutine's
    qualname, not `Task.get_name()` -- the latter is just `Task-<n>` unless
    someone passed `name=`, which would make the check vacuously true."""
    return getattr(task.get_coro(), "__qualname__", "") == "watch_schema_epoch"


def _free_port() -> int:
    """An ephemeral port the OS has just released. Racy in principle, fine
    here: nothing else in the suite binds one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StubSchemaCache:
    """Just enough `SchemaCache` for the watcher loop to spin harmlessly: no
    epoch ever, so `sync_schema_if_stale` is a no-op every tick."""

    async def get_epoch(self) -> str | None:
        return None


@pytest.fixture
def worker_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`app.jobs.worker` with `build_components` returning a bare
    `Metrics()` (its own `CollectorRegistry`, so no cross-test collector
    collisions) plus a stub schema cache, and `close_components` a no-op."""
    from app.jobs import worker

    async def fake_build(_settings: Settings) -> Any:
        return SimpleNamespace(metrics=Metrics(), schema_cache=_StubSchemaCache())

    async def fake_close(_components: Any) -> None:
        return None

    monkeypatch.setattr(worker, "build_components", fake_build)
    monkeypatch.setattr(worker, "close_components", fake_close)
    return worker


async def test_startup_serves_the_components_registry_and_shutdown_stops_it(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    monkeypatch.setattr(
        worker_module,
        "_settings",
        Settings(
            _env_file=None,
            metrics_enabled=True,
            worker_metrics_port=port,
            # Short enough that the loop really runs a tick or two while the
            # HTTP assertions below are in flight -- so a watcher that blows
            # up on the components it was handed fails this test rather than
            # dying quietly after it.
            schema_sync_poll_s=0.05,
        ),
    )

    ctx: dict[Any, Any] = {}
    await worker_module.startup(ctx)

    server = ctx["metrics_server"]
    assert server.port == port
    watch = ctx["schema_watch"]
    assert not watch.done()

    url = f"http://127.0.0.1:{port}/metrics"
    async with httpx.AsyncClient() as client:
        body = (await client.get(url, timeout=5)).text
        # A `t2s_*` collector only the worker's own registry carries --
        # proof it is *that* registry on the wire, not prometheus_client's
        # process-global default one.
        assert "t2s_sql_execution_total" in body

        await worker_module.shutdown(ctx)

        assert "metrics_server" not in ctx
        assert "schema_watch" not in ctx
        assert "components" not in ctx
        # Cancelled *and* awaited, so the loop cannot touch the components
        # `close_components` has just disposed -- and pytest never reports a
        # pending task destroyed at teardown.
        assert watch.cancelled()
        # See tests/unit/test_exporter.py for why this is `TransportError`
        # and not `ConnectError`.
        with pytest.raises(httpx.TransportError):
            await client.get(url, timeout=1)


async def test_startup_attaches_an_exit_log_to_the_watcher(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's watcher carries the same `log_task_exit` done-callback
    `app.main`'s lifespan attaches to its copy of the loop: without it, a
    watcher that died on its own would pin this worker to a stale schema
    with nothing in the log to say why."""
    monkeypatch.setattr(
        worker_module,
        "_settings",
        Settings(
            _env_file=None,
            metrics_enabled=False,
            worker_metrics_port=0,
            schema_sync_poll_s=0.05,
        ),
    )
    named: list[str] = []
    fired: list[Any] = []

    def fake_log_task_exit(name: str) -> Any:
        named.append(name)
        return fired.append

    monkeypatch.setattr(worker_module, "log_task_exit", fake_log_task_exit)

    ctx: dict[Any, Any] = {}
    await worker_module.startup(ctx)
    watch = ctx["schema_watch"]
    assert named == ["schema epoch watcher"]

    await worker_module.shutdown(ctx)
    # Done-callbacks are scheduled with `call_soon`, so they land on the
    # loop pass after the task finishes -- not inside `shutdown`'s `await`.
    await asyncio.sleep(0)
    assert fired == [watch]


@pytest.mark.parametrize(
    ("metrics_enabled", "worker_metrics_port"),
    [(True, 0), (False, 9100)],
    ids=["port-zero-disables", "metrics-disabled"],
)
async def test_startup_serves_nothing_when_disabled(
    worker_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    metrics_enabled: bool,
    worker_metrics_port: int,
) -> None:
    monkeypatch.setattr(
        worker_module,
        "_settings",
        Settings(
            _env_file=None,
            metrics_enabled=metrics_enabled,
            worker_metrics_port=worker_metrics_port,
        ),
    )

    ctx: dict[Any, Any] = {}
    await worker_module.startup(ctx)
    assert "metrics_server" not in ctx
    assert "components" in ctx

    # `shutdown` must not trip over the missing server.
    await worker_module.shutdown(ctx)
    assert ctx == {}


async def test_startup_skips_the_watcher_when_polling_is_disabled(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker_module,
        "_settings",
        Settings(
            _env_file=None,
            metrics_enabled=False,
            worker_metrics_port=0,
            schema_sync_poll_s=0,
        ),
    )

    ctx: dict[Any, Any] = {}
    await worker_module.startup(ctx)
    assert "schema_watch" not in ctx
    # And not merely unrecorded: a watcher started but left out of `ctx`
    # would outlive `shutdown`'s `close_components`.
    assert not [t for t in asyncio.all_tasks() if _is_watcher(t)]

    await worker_module.shutdown(ctx)
    assert ctx == {}
