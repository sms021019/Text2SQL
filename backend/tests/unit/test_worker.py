"""`app.jobs.worker`'s `on_startup`/`on_shutdown` wiring for the worker's own
Prometheus exposition: the arq worker has no ASGI app, so `startup` is the
only place that can put `AppComponents.metrics.registry` on the wire, and
`shutdown` is the only place that can take it back off.

Wiring only -- no arq, no Redis, no containers. `build_components` and
`close_components` are monkeypatched out, so what is under test is exactly
the `metrics_enabled and worker_metrics_port > 0` decision and the paired
`MetricsServer.close()`.

`worker.py` reads `_settings = get_settings()` at import time, so these
patch `app.jobs.worker._settings` with a fresh `Settings(_env_file=None,
...)` rather than the environment -- an env var would be too late.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.observability.metrics import Metrics


def _free_port() -> int:
    """An ephemeral port the OS has just released. Racy in principle, fine
    here: nothing else in the suite binds one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def worker_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`app.jobs.worker` with `build_components` returning a bare
    `Metrics()` (its own `CollectorRegistry`, so no cross-test collector
    collisions) and `close_components` a no-op."""
    from app.jobs import worker

    async def fake_build(_settings: Settings) -> Any:
        return SimpleNamespace(metrics=Metrics())

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
        Settings(_env_file=None, metrics_enabled=True, worker_metrics_port=port),
    )

    ctx: dict[Any, Any] = {}
    await worker_module.startup(ctx)

    server = ctx["metrics_server"]
    assert server.port == port

    url = f"http://127.0.0.1:{port}/metrics"
    async with httpx.AsyncClient() as client:
        body = (await client.get(url, timeout=5)).text
        # A `t2s_*` collector only the worker's own registry carries --
        # proof it is *that* registry on the wire, not prometheus_client's
        # process-global default one.
        assert "t2s_sql_execution_total" in body

        await worker_module.shutdown(ctx)

        assert "metrics_server" not in ctx
        assert "components" not in ctx
        # See tests/unit/test_exporter.py for why this is `TransportError`
        # and not `ConnectError`.
        with pytest.raises(httpx.TransportError):
            await client.get(url, timeout=1)


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
