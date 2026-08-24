"""`RedisCache`: an async, fail-open wrapper around `redis.asyncio`.

Every public method swallows `redis.exceptions.RedisError` and `OSError` --
a Redis outage must never surface as an exception to a caller, only as a
cache miss/no-op (see the Phase 2 plan's Global Constraints: "Cache is
transparent: with Redis down the API must still answer (log + miss)"). This
module is the one place allowed to import `redis`; `app/core/**` never does.
"""

from __future__ import annotations

import json
from typing import Any, cast

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

__all__ = ["RedisCache"]

logger = structlog.get_logger(__name__)

_SCAN_BATCH = 500


class RedisCache:
    """Namespaced JSON/bytes cache backed by Redis, with graceful
    degradation on connection failure.

    All keys passed to the public methods are the *unprefixed* logical keys
    produced by `app.cache.keys` (e.g. `"sql:<hash>"`); this class adds the
    `f"{namespace}:"` prefix itself before talking to Redis.
    """

    def __init__(self, url: str, *, namespace: str = "t2s", enabled: bool = True) -> None:
        self._namespace = namespace
        self._enabled = enabled
        self._last_error: str | None = None
        self._warned: set[str] = set()
        # Client construction is synchronous and lazy -- `from_url` doesn't
        # connect until the first command, so this is safe to call even when
        # `enabled=False` or the URL is unreachable.
        self._client: aioredis.Redis = aioredis.from_url(
            url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=False,
        )

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _prefixed(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def _record_error(self, err: Exception) -> None:
        message = str(err)
        self._last_error = message
        if message not in self._warned:
            self._warned.add(message)
            logger.warning("redis_cache_error", error=message)

    async def get_json(self, key: str) -> Any | None:
        raw = await self._get_raw(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as err:
            # A corrupt/foreign value under our key shouldn't raise either --
            # treat it the same as a miss.
            self._record_error(err)
            return None

    async def set_json(self, key: str, value: Any, *, ttl_s: int) -> bool:
        try:
            raw = json.dumps(value).encode()
        except (TypeError, ValueError) as err:
            self._record_error(err)
            return False
        return await self._set_raw(key, raw, ttl_s=ttl_s)

    async def get_bytes(self, key: str) -> bytes | None:
        return await self._get_raw(key)

    async def set_bytes(self, key: str, value: bytes, *, ttl_s: int) -> bool:
        return await self._set_raw(key, value, ttl_s=ttl_s)

    async def delete(self, *keys: str) -> int:
        """Delete zero or more exact (unprefixed) keys, e.g.
        `"sql:<hash>"`/`"res:<hash>"`; returns the count actually removed.
        Fail-open like every other public method here -- an unreachable
        Redis logs and returns `0`, never raises.
        """
        if not self._enabled or not keys:
            return 0
        prefixed = [self._prefixed(k) for k in keys]
        try:
            result: int = await self._client.unlink(*prefixed)
            return result
        except (RedisError, OSError):
            pass
        try:
            result = await self._client.delete(*prefixed)
            return result
        except (RedisError, OSError) as err:
            self._record_error(err)
            return 0

    async def delete_prefix(self, prefix: str) -> int:
        if not self._enabled:
            return 0
        pattern = self._prefixed(f"{prefix}*")
        deleted = 0
        try:
            batch: list[bytes] = []
            async for raw_key in self._client.scan_iter(match=pattern, count=_SCAN_BATCH):
                batch.append(raw_key)
                if len(batch) >= _SCAN_BATCH:
                    deleted += await self._unlink_batch(batch)
                    batch = []
            if batch:
                deleted += await self._unlink_batch(batch)
        except (RedisError, OSError) as err:
            self._record_error(err)
            return deleted
        return deleted

    async def _unlink_batch(self, keys: list[bytes]) -> int:
        try:
            result: int = await self._client.unlink(*keys)
        except (RedisError, OSError):
            # `UNLINK` isn't available on very old servers -- fall back to
            # the always-present `DEL`.
            result = await self._client.delete(*keys)
        return result

    async def ping(self) -> bool:
        if not self._enabled:
            return False
        try:
            result: bool = await self._client.ping()
            return bool(result)
        except (RedisError, OSError) as err:
            self._record_error(err)
            return False

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except (RedisError, OSError) as err:
            self._record_error(err)

    async def _get_raw(self, key: str) -> bytes | None:
        if not self._enabled:
            return None
        try:
            # `decode_responses=False` means the runtime type is always
            # `bytes | None`; the stub's broader `bytes | str | None` return
            # type only applies when `decode_responses` is a plain `bool`
            # that mypy can't narrow at the constructor call site.
            raw = await self._client.get(self._prefixed(key))
            return cast("bytes | None", raw)
        except (RedisError, OSError) as err:
            self._record_error(err)
            return None

    async def _set_raw(self, key: str, value: bytes, *, ttl_s: int) -> bool:
        if not self._enabled:
            return False
        try:
            ok = await self._client.set(self._prefixed(key), value, ex=ttl_s)
            return bool(ok)
        except (RedisError, OSError) as err:
            self._record_error(err)
            return False
