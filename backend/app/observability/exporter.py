"""Serve a `CollectorRegistry` over HTTP from a process that has no ASGI
app -- the arq worker (`app.jobs.worker`).

`app.main` exposes the API's registry through
prometheus-fastapi-instrumentator; there is no equivalent for a worker, so
its `t2s_*` series were recorded into a registry nothing scraped. This is
the same Prometheus exposition format served off a plain WSGI daemon thread
(`prometheus_client.start_http_server`), which is all a scrape target needs
-- see ADR 0005 and the `text2sql-worker` job in
`deploy/prometheus/prometheus.yml`.

The thread is a daemon and the handler is `prometheus_client`'s own silent
one, so exposition never blocks shutdown and never interleaves its own
access log with the app's structured JSON.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from wsgiref.simple_server import WSGIServer

from prometheus_client import CollectorRegistry, start_http_server

__all__ = ["MetricsServer", "start_metrics_server"]


@dataclass
class MetricsServer:
    """A running exposition server. `port` is the *bound* port, which
    matters when the caller asked for `0` (tests) and needs to know where
    the OS actually put it."""

    port: int
    _httpd: WSGIServer
    _thread: threading.Thread
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        """Stop serving and join the thread. Idempotent: the worker's
        `on_shutdown` may run after an already-failed startup, and
        `shutdown()`/`server_close()` on a closed server would raise."""
        if self._closed:
            return
        self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def start_metrics_server(
    registry: CollectorRegistry, *, port: int, addr: str = "0.0.0.0"
) -> MetricsServer:
    """Serve `registry` at `http://addr:port/metrics` on a daemon thread.

    `port=0` binds a free port chosen by the OS -- read it back off the
    returned `MetricsServer.port`.
    """
    httpd, thread = start_http_server(port, addr=addr, registry=registry)
    return MetricsServer(port=httpd.server_port, _httpd=httpd, _thread=thread)
