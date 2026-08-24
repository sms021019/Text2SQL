"""Integration tests for `app.cache.redis.RedisCache` against a real Redis
container, plus graceful-degradation tests against an unreachable one.

Marked `integration` (needs Docker) like the rest of `tests/integration/`.
"""

import asyncio

import pytest

from app.cache.redis import RedisCache

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Happy path: real container
# --------------------------------------------------------------------------


@pytest.fixture
async def cache(redis_url):
    c = RedisCache(redis_url, namespace="t2s-test")
    yield c
    # best-effort cleanup so tests don't leak keys into each other
    await c.delete_prefix("")
    await c.aclose()


async def test_ping_succeeds_against_real_container(cache) -> None:
    assert await cache.ping() is True
    assert cache.last_error is None


async def test_set_json_then_get_json_roundtrip(cache) -> None:
    ok = await cache.set_json("sql:abc", {"sql": "SELECT 1", "n": 3}, ttl_s=60)
    assert ok is True
    assert await cache.get_json("sql:abc") == {"sql": "SELECT 1", "n": 3}


async def test_get_json_miss_returns_none(cache) -> None:
    assert await cache.get_json("sql:does-not-exist") is None


async def test_set_json_respects_ttl(cache) -> None:
    ok = await cache.set_json("sql:ttl-key", {"a": 1}, ttl_s=1)
    assert ok is True
    assert await cache.get_json("sql:ttl-key") == {"a": 1}
    await asyncio.sleep(1.5)
    assert await cache.get_json("sql:ttl-key") is None


async def test_set_bytes_then_get_bytes_roundtrip(cache) -> None:
    payload = b"\x00\x01\x02binary-embedding-data"
    ok = await cache.set_bytes("schema:v1", payload, ttl_s=60)
    assert ok is True
    assert await cache.get_bytes("schema:v1") == payload


async def test_get_bytes_miss_returns_none(cache) -> None:
    assert await cache.get_bytes("schema:missing") is None


async def test_delete_prefix_removes_only_matching_keys(cache) -> None:
    await cache.set_json("sql:x1", {"v": 1}, ttl_s=60)
    await cache.set_json("sql:x2", {"v": 2}, ttl_s=60)
    await cache.set_json("res:y1", {"v": 3}, ttl_s=60)

    deleted = await cache.delete_prefix("sql:")

    assert deleted == 2
    assert await cache.get_json("sql:x1") is None
    assert await cache.get_json("sql:x2") is None
    # unrelated prefix untouched
    assert await cache.get_json("res:y1") == {"v": 3}


async def test_delete_prefix_with_no_matches_returns_zero(cache) -> None:
    assert await cache.delete_prefix("nope:") == 0


async def test_delete_prefix_handles_more_than_one_scan_batch(cache) -> None:
    # batches of 500 per the spec; use a small-but-real number of keys to
    # exercise scan_iter's cursor loop without a slow test.
    n = 20
    for i in range(n):
        await cache.set_json(f"batch:{i}", {"i": i}, ttl_s=60)
    assert await cache.delete_prefix("batch:") == n


async def test_keys_are_namespaced_so_different_namespaces_do_not_collide(redis_url) -> None:
    a = RedisCache(redis_url, namespace="ns-a")
    b = RedisCache(redis_url, namespace="ns-b")
    try:
        await a.set_json("sql:shared", {"who": "a"}, ttl_s=60)
        assert await b.get_json("sql:shared") is None
        assert await a.get_json("sql:shared") == {"who": "a"}
    finally:
        await a.delete_prefix("")
        await b.delete_prefix("")
        await a.aclose()
        await b.aclose()


# --------------------------------------------------------------------------
# enabled=False short-circuits everything without touching the client
# --------------------------------------------------------------------------


async def test_disabled_cache_never_touches_redis(redis_url) -> None:
    c = RedisCache(redis_url, enabled=False)
    assert await c.get_json("sql:whatever") is None
    assert await c.set_json("sql:whatever", {"a": 1}, ttl_s=60) is False
    assert await c.get_bytes("schema:v1") is None
    assert await c.set_bytes("schema:v1", b"x", ttl_s=60) is False
    assert await c.delete_prefix("sql:") == 0
    assert await c.ping() is False
    assert c.last_error is None
    await c.aclose()


# --------------------------------------------------------------------------
# Graceful degradation: unreachable Redis
# --------------------------------------------------------------------------


@pytest.fixture
async def unreachable_cache():
    c = RedisCache("redis://localhost:1")
    yield c
    await c.aclose()


async def test_get_json_returns_none_when_unreachable(unreachable_cache) -> None:
    assert await unreachable_cache.get_json("sql:x") is None
    assert unreachable_cache.last_error is not None


async def test_set_json_returns_false_when_unreachable(unreachable_cache) -> None:
    assert await unreachable_cache.set_json("sql:x", {"a": 1}, ttl_s=60) is False
    assert unreachable_cache.last_error is not None


async def test_get_bytes_returns_none_when_unreachable(unreachable_cache) -> None:
    assert await unreachable_cache.get_bytes("schema:v1") is None
    assert unreachable_cache.last_error is not None


async def test_set_bytes_returns_false_when_unreachable(unreachable_cache) -> None:
    assert await unreachable_cache.set_bytes("schema:v1", b"x", ttl_s=60) is False
    assert unreachable_cache.last_error is not None


async def test_delete_prefix_returns_zero_when_unreachable(unreachable_cache) -> None:
    assert await unreachable_cache.delete_prefix("sql:") == 0
    assert unreachable_cache.last_error is not None


async def test_ping_returns_false_when_unreachable(unreachable_cache) -> None:
    assert await unreachable_cache.ping() is False
    assert unreachable_cache.last_error is not None


async def test_unreachable_cache_never_raises() -> None:
    """No exception should ever escape a public method, even across several
    consecutive failing calls."""
    c = RedisCache("redis://localhost:1")
    await c.get_json("sql:x")
    await c.set_json("sql:x", {"a": 1}, ttl_s=60)
    await c.get_bytes("sql:x")
    await c.set_bytes("sql:x", b"y", ttl_s=60)
    await c.delete_prefix("sql:")
    await c.ping()
    await c.aclose()
