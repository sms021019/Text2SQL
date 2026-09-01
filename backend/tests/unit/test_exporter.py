"""`start_metrics_server` must actually serve the registry it is handed --
the arq worker has no ASGI app, so this thread-backed exposition is the only
way Prometheus sees its `t2s_*` series (`app/observability/exporter.py`).

`port=0` lets the OS pick a free port, so the test never collides with a
real listener; `MetricsServer.port` reports the bound one.
"""

from __future__ import annotations

import httpx
import pytest
from prometheus_client import CollectorRegistry, Counter

from app.observability.exporter import start_metrics_server


def test_metrics_server_serves_the_given_registry_and_closes() -> None:
    registry = CollectorRegistry()
    Counter("t2s_test_total", "test", registry=registry).inc(3)

    server = start_metrics_server(registry, port=0, addr="127.0.0.1")
    try:
        assert server.port > 0
        body = httpx.get(f"http://127.0.0.1:{server.port}/metrics", timeout=5).text
        assert "t2s_test_total 3.0" in body
    finally:
        server.close()
        server.close()  # idempotent

    # `TransportError`, not `ConnectError`: what a connect to the now-dead
    # port raises is OS-dependent. Linux/macOS answer the SYN with an RST
    # (`ConnectError`); a Windows host whose firewall silently drops inbound
    # packets to a port nothing is listening on times out instead
    # (`ConnectTimeout`). Both are `httpx.TransportError`, and either one
    # proves the same thing -- nothing is serving there any more.
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{server.port}/metrics", timeout=1)
