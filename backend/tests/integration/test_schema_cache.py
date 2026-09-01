"""Integration tests for `app.cache.schema_cache.SchemaCache` and its
lifespan/refresh wiring (`app.services.schema_service.prepare_retriever`,
used from both `app.main`'s lifespan and `POST /api/v1/schema/refresh`):
cold start persists the schema graph + embedding index to Redis, a warm
second start skips the LLM `embed()` call entirely, and `/schema/refresh`
invalidates then re-stores.

Uses the real seeded container database (`app_db_url`/`readonly_async_url`)
and the real `redis_url` container, but a `FakeLLM` for every completion, in
`app.main.create_app()`'s actual lifespan -- mirrors `test_api.py`.
"""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.cache.redis import RedisCache
from app.cache.schema_cache import SchemaCache
from app.config import Settings
from app.main import create_app
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(app_db_url: str, readonly_async_url: str, redis_url: str) -> Settings:
    return Settings(
        app_db_url=app_db_url,
        target_db_url=readonly_async_url,
        redis_url=redis_url,
        _env_file=None,
    )


@pytest.fixture(autouse=True)
async def _clean_schema_cache(redis_url: str):
    # All tests in this module share the session-scoped `redis_url`
    # container and the default "t2s" namespace `create_app()` uses -- and
    # every test seeds the *same* target DB, so introspection always
    # produces the same `graph.version`. Without this, an earlier test's
    # stored keys would look like a cache hit to a later one.
    cleaner = RedisCache(redis_url)
    await cleaner.delete_prefix("schema:")
    yield
    await cleaner.delete_prefix("schema:")
    await cleaner.aclose()


async def test_cold_start_embeds_once_and_stores_graph_and_index_in_redis(
    settings: Settings,
) -> None:
    llm = FakeLLM()
    app = create_app(settings=settings, llm=llm)

    async with LifespanManager(app):
        version = app.state.graph.version
        assert app.state.schema_index_source == "embedded"
        assert app.state.retriever.export_index() is not None

    assert len(llm.embed_calls) == 1

    checker = RedisCache(settings.redis_url)
    try:
        cache = SchemaCache(checker, ttl_s=settings.schema_cache_ttl_s)
        loaded = await cache.load(version)
        assert loaded is not None
        graph, names, matrix = loaded
        assert graph.version == version
        assert "orders" in names
        assert matrix.shape == (len(names), matrix.shape[1])
    finally:
        await checker.aclose()


async def test_warm_start_with_same_settings_hits_cache_and_makes_zero_embed_calls(
    settings: Settings,
) -> None:
    cold_llm = FakeLLM()
    cold_app = create_app(settings=settings, llm=cold_llm)
    async with LifespanManager(cold_app):
        version = cold_app.state.graph.version
    assert len(cold_llm.embed_calls) == 1

    warm_llm = FakeLLM()
    warm_app = create_app(settings=settings, llm=warm_llm)
    async with LifespanManager(warm_app):
        assert warm_app.state.graph.version == version
        assert warm_app.state.schema_index_source == "cache"
        exported = warm_app.state.retriever.export_index()
        assert exported is not None
        names, _matrix = exported
        assert "orders" in names

    assert len(warm_llm.embed_calls) == 0


async def test_schema_refresh_invalidates_and_restores_cache(settings: Settings) -> None:
    llm = FakeLLM()
    app = create_app(settings=settings, llm=llm)

    async with LifespanManager(app):
        version = app.state.graph.version
        assert len(llm.embed_calls) == 1  # cold-start build

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/schema/refresh")
        assert resp.status_code == 200
        assert resp.json() == {"version": version}

        # refresh() invalidated the cache and rebuilt from scratch -- a
        # second embed() call, not a cache hit.
        assert len(llm.embed_calls) == 2
        assert app.state.schema_index_source == "embedded"
        assert app.state.retriever.export_index() is not None

    checker = RedisCache(settings.redis_url)
    try:
        cache = SchemaCache(checker, ttl_s=settings.schema_cache_ttl_s)
        loaded = await cache.load(version)
        assert loaded is not None
    finally:
        await checker.aclose()


async def test_load_with_unknown_version_is_a_miss(redis_url: str) -> None:
    checker = RedisCache(redis_url)
    try:
        cache = SchemaCache(checker, ttl_s=60)
        assert await cache.load("no-such-version") is None
    finally:
        await checker.aclose()
