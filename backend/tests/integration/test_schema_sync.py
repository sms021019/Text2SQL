"""Integration tests for `app.services.schema_sync`: a schema refresh run in
one process reaching every other process that shares the same Redis.

Both "processes" here are two runtimes built in one pytest process against
the *same* container Redis and the *same* seeded target database -- which is
exactly the situation the epoch mechanism exists for (two API replicas, or
the API and the arq worker). Since the seeded schema never changes, both
runtimes always introspect to the same `graph.version`, so version equality
proves nothing; the signal that a refresh actually ran is the *identity* of
the `SchemaGraph` object (`refresh_components` swaps in a brand-new one).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.cache.redis import RedisCache
from app.config import Settings
from app.main import create_app
from app.services.bootstrap import build_components, close_components, refresh_components
from app.services.schema_sync import mark_refreshed, sync_schema_if_stale
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
async def _clean_schema_keys(redis_url: str) -> AsyncIterator[None]:
    """Drop every `schema:` key -- the cached graph/index *and* the epoch --
    before and after each test.

    The `redis_url` container is session-scoped and shared with the other
    integration modules, so without this an epoch left behind by an earlier
    module would already differ from a freshly built runtime's `None`
    baseline and turn the "nothing happened yet" assertions into a refresh.
    """
    cleaner = RedisCache(redis_url)
    await cleaner.delete_prefix("schema:")
    yield
    await cleaner.delete_prefix("schema:")
    await cleaner.aclose()


async def test_refresh_in_one_runtime_propagates_to_another(settings: Settings) -> None:
    b_llm = FakeLLM()
    a = await build_components(settings, llm=FakeLLM())
    b = await build_components(settings, llm=b_llm)
    try:
        old_graph_b = b.graph
        assert await sync_schema_if_stale(b) is False  # nothing happened yet

        await refresh_components(a)
        await mark_refreshed(a)
        assert a.schema_epoch is not None
        assert a.schema_index_source == "embedded"  # the refresher rebuilds the index

        # The follower must adopt the index `a` just stored, not sweep it and
        # re-embed: a propagated refresh costs one build plus N-1 cache hits,
        # so `b`'s LLM sees no `embed()` call at all across the sync.
        embeds_before = len(b_llm.embed_calls)
        assert await sync_schema_if_stale(b) is True
        assert len(b_llm.embed_calls) == embeds_before
        assert b.schema_index_source == "cache"
        assert await b.schema_cache.load(b.graph.version) is not None  # index survived

        assert b.schema_epoch == a.schema_epoch
        assert b.graph is not old_graph_b  # rebuilt
        assert await sync_schema_if_stale(b) is False  # idempotent
        assert await sync_schema_if_stale(a) is False  # no re-refresh for its own bump
    finally:
        await close_components(a)
        await close_components(b)


async def test_sync_is_a_noop_without_redis(settings: Settings) -> None:
    dead = settings.model_copy(update={"redis_url": "redis://localhost:1"})
    c = await build_components(dead, llm=FakeLLM())
    try:
        assert c.schema_epoch is None
        assert await sync_schema_if_stale(c) is False
        await mark_refreshed(c)  # must not raise
    finally:
        await close_components(c)


async def test_invalidate_all_keeps_the_epoch(settings: Settings) -> None:
    c = await build_components(settings, llm=FakeLLM())
    try:
        await mark_refreshed(c)
        await c.schema_cache.invalidate_all()
        assert await c.schema_cache.get_epoch() == c.schema_epoch
    finally:
        await close_components(c)


async def test_post_schema_refresh_reaches_a_second_api_process(settings: Settings) -> None:
    fast = settings.model_copy(update={"schema_sync_poll_s": 0.2})
    app_a = create_app(settings=fast, llm=FakeLLM())
    app_b = create_app(settings=fast, llm=FakeLLM())
    async with LifespanManager(app_a), LifespanManager(app_b):
        b_before = app_b.state.components.graph
        async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as c:
            assert (await c.post("/api/v1/schema/refresh")).status_code == 200
        for _ in range(50):  # <= 5 s
            if app_b.state.components.graph is not b_before:
                break
            await asyncio.sleep(0.1)
        assert app_b.state.components.graph is not b_before
        assert app_b.state.graph is app_b.state.components.graph  # publish_components ran
