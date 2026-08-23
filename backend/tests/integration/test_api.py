"""Integration tests for the FastAPI app: `create_app()` wired to a real
seeded container database (via `readonly_async_url`/`app_db_url`) but a
`FakeLLM` for every completion.

`httpx.ASGITransport` does not run the app's lifespan, so every test drives
it explicitly with `asgi_lifespan.LifespanManager` -- otherwise `app.state`
would never get `pipeline`/`graph`/etc. and every request would 503.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import Settings
from app.db.models import QueryLog
from app.main import create_app
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration

GOOD_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM orders", "explanation": "Counts all orders."}
)


@pytest.fixture
def settings(app_db_url: str, readonly_async_url: str) -> Settings:
    return Settings(app_db_url=app_db_url, target_db_url=readonly_async_url, _env_file=None)


@pytest.fixture
async def client(
    settings: Settings, responses: list[str] | dict[str, str]
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=settings, llm=FakeLLM(responses))
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def responses() -> list[str]:
    """Default `FakeLLM` script -- overridden per-test via indirect
    parametrization where a specific response sequence is needed."""
    return []


async def test_healthz_always_200(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_200_after_lifespan(client: AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_get_schema(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/schema")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["version"], str) and body["version"]

    tables = {t["name"]: t for t in body["tables"]}
    assert "orders" in tables
    orders_cols = {c["name"]: c for c in tables["orders"]["columns"]}
    assert orders_cols["id"]["is_pk"] is True
    assert orders_cols["id"]["nullable"] is False


async def test_schema_refresh_returns_same_version_for_unchanged_schema(
    client: AsyncClient,
) -> None:
    before = (await client.get("/api/v1/schema")).json()
    resp = await client.post("/api/v1/schema/refresh")
    assert resp.status_code == 200
    assert resp.json() == {"version": before["version"]}

    after = (await client.get("/api/v1/schema")).json()
    assert after["version"] == before["version"]


@pytest.mark.parametrize("responses", [[GOOD_RESPONSE]])
async def test_query_happy_path(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/query", json={"question": "How many orders are there?"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["error"] is None
    assert body["repaired"] is False
    assert body["rows"] == [[20000]]
    assert body["row_count"] == 1
    assert body["columns"] == ["n"]
    assert body["truncated"] is False
    assert "orders" in body["tables"]
    assert body["explanation"] == "Counts all orders."
    assert body["usage"]["prompt_tokens"] > 0
    assert [t["stage"] for t in body["timings"]] == [
        "retrieve",
        "render",
        "generate",
        "parse",
        "guard",
        "execute",
    ]
    assert body["request_id"]


@pytest.mark.parametrize("responses", [["DELETE FROM orders"]])
async def test_query_guard_rejection_returns_200_with_error(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/query", json={"question": "Delete all orders"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["error"] is not None
    assert body["error"].startswith("guard:not_select")
    assert body["rows"] == []
    assert body["columns"] == []
    assert body["row_count"] == 0


async def test_query_empty_question_is_422(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/query", json={"question": ""})
    assert resp.status_code == 422


async def test_request_id_is_echoed_back(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Request-ID": "test-request-id-123"})
    assert resp.headers["X-Request-ID"] == "test-request-id-123"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.headers["X-Request-ID"]


@pytest.mark.parametrize("responses", [[GOOD_RESPONSE]])
async def test_query_writes_a_query_log_row(
    client: AsyncClient, app_db_url: str
) -> None:
    resp = await client.post("/api/v1/query", json={"question": "How many orders are there?"})
    request_id = resp.json()["request_id"]

    engine = create_async_engine(app_db_url)
    try:
        async with AsyncSession(engine) as session:
            row = (
                await session.execute(
                    select(QueryLog).where(QueryLog.request_id == request_id)
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert row.question == "How many orders are there?"
    assert row.success is True
    assert row.sql is not None
