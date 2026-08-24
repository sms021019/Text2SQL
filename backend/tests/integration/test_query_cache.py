"""Integration tests for the two-tier SQL/result query cache
(`app.cache.query_cache.QueryCache`) wired into `Text2SQLPipeline` via
`app.services.schema_service.build_pipeline`, exercised through the real
`create_app()` lifespan against the seeded container database and the real
`redis_url` container, with a `FakeLLM` standing in for every completion --
mirrors `test_api.py`/`test_schema_cache.py`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.cache.redis import RedisCache
from app.config import Settings
from app.llm.base import Usage
from app.main import create_app
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration

GOOD_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM orders", "explanation": "Counts all orders."}
)
OTHER_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM customers", "explanation": "Counts all customers."}
)
DELETE_RESPONSE = json.dumps({"sql": "DELETE FROM orders", "explanation": "Deletes all orders."})


class RecordingObserver:
    """A `PipelineObserver` (structurally) that just records every call, in
    order, for the happy-path event-sequence assertion below."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def on_stage(self, stage: str, ms: float) -> None:
        self.events.append(("stage", {"stage": stage}))

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        self.events.append(("llm", {"stage": stage, "model": model}))

    def on_guard_reject(self, reason: str) -> None:
        self.events.append(("guard_reject", {"reason": reason}))

    def on_execution(self, *, outcome: str, ms: float) -> None:
        self.events.append(("execution", {"outcome": outcome}))

    def on_cache(self, *, cache: str, outcome: str) -> None:
        self.events.append(("cache", {"cache": cache, "outcome": outcome}))

    def on_pipeline_done(self, *, ms: float, ok: bool) -> None:
        self.events.append(("pipeline_done", {"ok": ok}))


@pytest.fixture
def settings(app_db_url: str, readonly_async_url: str, redis_url: str) -> Settings:
    return Settings(
        app_db_url=app_db_url, target_db_url=readonly_async_url, redis_url=redis_url, _env_file=None
    )


@pytest.fixture(autouse=True)
async def _clean_query_cache(redis_url: str) -> AsyncIterator[None]:
    # All tests in this module share the session-scoped `redis_url`
    # container and default "t2s" namespace, and every test seeds the same
    # target DB (so `graph.version` -- baked into every cache key -- is
    # stable across tests) -- without this, an earlier test's stored keys
    # would look like a hit to a later one.
    cleaner = RedisCache(redis_url)
    await cleaner.delete_prefix("sql:")
    await cleaner.delete_prefix("res:")
    yield
    await cleaner.delete_prefix("sql:")
    await cleaner.delete_prefix("res:")
    await cleaner.aclose()


@pytest.fixture
async def client_and_llm(settings: Settings) -> AsyncIterator[tuple[AsyncClient, FakeLLM]]:
    llm = FakeLLM([GOOD_RESPONSE, OTHER_RESPONSE, GOOD_RESPONSE])
    app = create_app(settings=settings, llm=llm)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, llm


async def test_second_identical_question_is_sql_hit_with_one_llm_call_total(
    client_and_llm: tuple[AsyncClient, FakeLLM],
) -> None:
    client, llm = client_and_llm
    body = {"question": "How many orders are there?"}

    first = await client.post("/api/v1/query", json=body)
    assert first.status_code == 200
    assert first.json()["cache_status"] == "miss"

    second = await client.post("/api/v1/query", json=body)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["cache_status"] == "sql_hit"
    assert second_body["rows"] == first.json()["rows"]
    assert second_body["error"] is None

    assert len(llm.calls) == 1


async def test_result_cache_hit_returns_complete_output_with_no_execute_stage(
    client_and_llm: tuple[AsyncClient, FakeLLM],
) -> None:
    client, llm = client_and_llm
    body = {"question": "How many orders are there?", "use_result_cache": True}

    first = await client.post("/api/v1/query", json=body)
    assert first.json()["cache_status"] == "miss"

    second = await client.post("/api/v1/query", json=body)
    second_body = second.json()
    assert second_body["cache_status"] == "result_hit"
    assert second_body["rows"] == first.json()["rows"]
    assert second_body["sql"] == first.json()["sql"]
    assert second_body["explanation"] == first.json()["explanation"]

    stages = [t["stage"] for t in second_body["timings"]]
    assert "execute" not in stages
    assert stages == ["result_cache"]

    assert second_body["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0.0,
    }

    assert len(llm.calls) == 1


async def test_different_question_is_a_miss(client_and_llm: tuple[AsyncClient, FakeLLM]) -> None:
    client, llm = client_and_llm

    r1 = await client.post("/api/v1/query", json={"question": "How many orders are there?"})
    r2 = await client.post("/api/v1/query", json={"question": "How many customers are there?"})

    assert r1.json()["cache_status"] == "miss"
    assert r2.json()["cache_status"] == "miss"
    assert len(llm.calls) == 2


async def test_guard_rejected_sql_is_never_cached(settings: Settings, redis_url: str) -> None:
    llm = FakeLLM([DELETE_RESPONSE])
    app = create_app(settings=settings, llm=llm)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/query", json={"question": "Delete all the orders"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_status"] == "miss"
    assert body["error"] is not None
    assert body["error"].startswith("guard:")

    checker = RedisCache(redis_url)
    try:
        # `delete_prefix` returns the count it deleted -- zero means nothing
        # was ever stored under "sql:" for this run.
        assert await checker.delete_prefix("sql:") == 0
        assert await checker.delete_prefix("res:") == 0
    finally:
        await checker.aclose()


async def test_use_cache_false_bypasses_cache_entirely(
    client_and_llm: tuple[AsyncClient, FakeLLM],
) -> None:
    client, llm = client_and_llm
    body = {"question": "How many orders are there?", "use_cache": False}

    first = await client.post("/api/v1/query", json=body)
    second = await client.post("/api/v1/query", json=body)

    assert first.json()["cache_status"] == "bypass"
    assert second.json()["cache_status"] == "bypass"
    assert len(llm.calls) == 2


async def test_redis_unreachable_falls_back_to_miss_and_still_answers(settings: Settings) -> None:
    unreachable = RedisCache("redis://localhost:1")
    llm = FakeLLM([GOOD_RESPONSE])
    app = create_app(settings=settings, llm=llm, redis=unreachable)

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/query", json={"question": "How many orders are there?"}
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_status"] == "miss"
    assert body["error"] is None


async def test_observer_sees_cache_and_execution_hooks_on_happy_path(settings: Settings) -> None:
    observer = RecordingObserver()
    llm = FakeLLM([GOOD_RESPONSE])
    app = create_app(settings=settings, llm=llm, observer=observer)

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/query", json={"question": "How many orders are there?"}
            )
    assert resp.status_code == 200

    # Startup (schema retrieval index build) also emits events on this same
    # observer -- only inspect the events from this one request onward by
    # anchoring on the pipeline-level cache-read pair, which only ever
    # happens inside `Text2SQLPipeline.run()`.
    cache_idx = observer.events.index(("cache", {"cache": "result", "outcome": "bypass"}))
    events = observer.events[cache_idx:]

    assert events[0] == ("cache", {"cache": "result", "outcome": "bypass"})
    assert events[1] == ("cache", {"cache": "sql", "outcome": "miss"})

    stage_names = [payload["stage"] for kind, payload in events if kind == "stage"]
    assert stage_names == ["retrieve", "render", "generate", "parse", "guard", "execute"]

    llm_events = [payload for kind, payload in events if kind == "llm"]
    assert llm_events == [{"stage": "generate", "model": "fake"}]

    execution_events = [payload for kind, payload in events if kind == "execution"]
    assert execution_events == [{"outcome": "success"}]

    assert events[-1] == ("pipeline_done", {"ok": True})
