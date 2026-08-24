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

from app.cache.keys import result_cache_key
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
        # Prove Redis is actually reachable and `delete_prefix`'s count is
        # trustworthy first -- otherwise a `0` below is indistinguishable
        # from Redis being down (see the graceful-degradation tests), which
        # would make this assertion vacuously true either way.
        assert await checker.ping() is True
        await checker.set_json("sql:sentinel", {"v": 1}, ttl_s=60)
        assert await checker.delete_prefix("sql:") == 1

        # ... and *now* zero really does mean nothing was ever stored under
        # "sql:"/"res:" for this run.
        assert await checker.delete_prefix("sql:") == 0
        assert await checker.delete_prefix("res:") == 0
    finally:
        await checker.aclose()


async def test_sql_hit_with_repairable_execution_failure_evicts_and_falls_through(
    settings: Settings, redis_url: str
) -> None:
    """A SQL-tier hit whose cached statement fails execution with a
    repairable `ExecutionError.kind` (here `"syntax"`, from Postgres'
    `undefined_table`) self-heals: the stale entry is evicted and the
    question falls through to a full retrieve-through-execute run -- see
    `Text2SQLPipeline._sql_hit`/`_should_evict`."""
    llm = FakeLLM([GOOD_RESPONSE])
    app = create_app(settings=settings, llm=llm)
    question = "How many orders are there?"

    async with LifespanManager(app):
        query_cache = app.state.query_cache
        key = query_cache.key_for(question)

        seeder = RedisCache(redis_url)
        try:
            # The guard only validates against *this cached entry's own*
            # `tables` list, not the live schema -- so listing
            # "nonexistent_table" here (as if it used to be a real table
            # before some past schema change) lets the statement clear the
            # guard, exactly as a genuinely stale cache entry would. The
            # database then rejects it for real: `undefined_table` (SQLSTATE
            # 42P01), classified as the repairable "syntax" kind.
            await seeder.set_json(
                key,
                {
                    "sql": "SELECT * FROM nonexistent_table",
                    "explanation": "stale cached answer",
                    "tables": ["orders", "nonexistent_table"],
                },
                ttl_s=60,
            )
        finally:
            await seeder.aclose()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/query", json={"question": question})

    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_status"] == "miss"
    assert body["error"] is None
    assert len(llm.calls) == 1

    checker = RedisCache(redis_url)
    try:
        replaced = await checker.get_json(key)
        assert replaced is not None
        assert replaced["sql"] != "SELECT * FROM nonexistent_table"
        # The evicted entry's result-tier sibling (there wasn't one seeded
        # here, but `delete_sql` always deletes both) is gone too.
        assert await checker.get_json(result_cache_key(sql_key=key)) is None
    finally:
        await checker.aclose()


async def test_sql_hit_with_guard_rejection_evicts_without_fallthrough(
    settings: Settings, redis_url: str
) -> None:
    """A SQL-tier hit whose cached statement is rejected by the guard (a
    policy that tightened since it was cached, or simply a poisoned entry)
    is evicted but does *not* fall through to a fresh generation -- a guard
    rejection is about the SQL text, not a stale-schema mismatch a
    regeneration would fix."""
    llm = FakeLLM([])
    app = create_app(settings=settings, llm=llm)
    question = "How many orders are there?"

    async with LifespanManager(app):
        query_cache = app.state.query_cache
        key = query_cache.key_for(question)

        seeder = RedisCache(redis_url)
        try:
            await seeder.set_json(
                key,
                {
                    "sql": "DELETE FROM orders",
                    "explanation": "stale cached answer",
                    "tables": ["orders"],
                },
                ttl_s=60,
            )
        finally:
            await seeder.aclose()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/query", json={"question": question})

    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_status"] == "sql_hit"
    assert body["error"] is not None
    assert body["error"].startswith("guard:")
    assert llm.calls == []

    checker = RedisCache(redis_url)
    try:
        assert await checker.get_json(key) is None
        assert await checker.get_json(result_cache_key(sql_key=key)) is None
    finally:
        await checker.aclose()


async def test_use_cache_false_emits_bypass_cache_events_on_both_tiers(settings: Settings) -> None:
    observer = RecordingObserver()
    llm = FakeLLM([GOOD_RESPONSE])
    app = create_app(settings=settings, llm=llm, observer=observer)

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/query",
                json={"question": "How many orders are there?", "use_cache": False},
            )

    assert resp.status_code == 200
    assert resp.json()["cache_status"] == "bypass"

    cache_events = [payload for kind, payload in observer.events if kind == "cache"]
    assert cache_events == [
        {"cache": "result", "outcome": "bypass"},
        {"cache": "sql", "outcome": "bypass"},
    ]


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

    # This observer is only ever passed to `Text2SQLPipeline` -- startup's
    # schema-retrieval index build (`prepare_retriever`/
    # `SchemaRetriever.build_index()`) never touches it, so `observer.events`
    # holds exactly this one request's `run()` call, from the start.
    events = observer.events

    assert events[0] == ("cache", {"cache": "result", "outcome": "bypass"})
    assert events[1] == ("cache", {"cache": "sql", "outcome": "miss"})

    stage_names = [payload["stage"] for kind, payload in events if kind == "stage"]
    assert stage_names == ["retrieve", "render", "generate", "parse", "guard", "execute"]

    llm_events = [payload for kind, payload in events if kind == "llm"]
    assert llm_events == [{"stage": "generate", "model": "fake"}]

    execution_events = [payload for kind, payload in events if kind == "execution"]
    assert execution_events == [{"outcome": "success"}]

    assert events[-1] == ("pipeline_done", {"ok": True})
