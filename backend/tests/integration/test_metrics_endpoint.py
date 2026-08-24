"""Integration test for `GET /metrics`: after driving one query through the
real `create_app()` lifespan (seeded container database, `FakeLLM`), the
Prometheus text exposition served at `/metrics` carries samples for the
pipeline, execution, and schema-cache metrics, plus HTTP request metrics for
an instrumented route but none for an excluded one (`/healthz`) -- mirrors
`test_api.py`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration

GOOD_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM orders", "explanation": "Counts all orders."}
)


@pytest.fixture
def settings(app_db_url: str, readonly_async_url: str, redis_url: str) -> Settings:
    return Settings(
        app_db_url=app_db_url, target_db_url=readonly_async_url, redis_url=redis_url, _env_file=None
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=settings, llm=FakeLLM([GOOD_RESPONSE]))
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        # `follow_redirects=True`: `app.mount("/metrics", ...)` 307s a
        # trailing-slash-less `GET /metrics` to `/metrics/` (Starlette's
        # default `Mount` behaviour), same as `/api/v1/schema/` would.
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=True
        ) as ac:
            yield ac


async def test_metrics_endpoint_reports_pipeline_execution_and_schema_cache_samples(
    client: AsyncClient,
) -> None:
    query_resp = await client.post("/api/v1/query", json={"question": "How many orders are there?"})
    assert query_resp.status_code == 200

    metrics_resp = await client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert metrics_resp.headers["content-type"].startswith("text/plain")

    body = metrics_resp.text

    assert "t2s_pipeline_duration_seconds_count" in body
    assert 't2s_sql_execution_total{outcome="success"} 1.0' in body

    # Startup's `prepare_retriever()` -- a cold cache, so `outcome="miss"` --
    # recorded exactly one `cache="schema"` sample.
    assert 't2s_cache_requests_total{cache="schema",outcome="miss"} 1.0' in body


async def test_metrics_endpoint_has_http_samples_for_instrumented_routes_only(
    client: AsyncClient,
) -> None:
    await client.post("/api/v1/query", json={"question": "How many orders are there?"})
    await client.get("/healthz")

    body = (await client.get("/metrics")).text

    assert "http_request_duration_seconds" in body
    assert "/api/v1/query" in body

    # `/healthz` is in `excluded_handlers` -- it must never appear as a
    # `handler` label value on the HTTP request metrics.
    for line in body.splitlines():
        if line.startswith("http_request_duration_seconds") and "handler=" in line:
            assert '/healthz"' not in line
