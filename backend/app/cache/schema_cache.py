"""`SchemaCache`: persists a `SchemaGraph` plus its retrieval embedding
index in Redis, keyed by schema version, so a warm restart (or another
process sharing the same Redis) can skip re-embedding every table summary
through the LLM.

Layout: for a given `version`, three keys under `schema:{version}` (see
`app.cache.keys.schema_cache_key`):
  - `schema:{version}:graph`  -- `SchemaGraph.model_dump_json()`, as bytes
  - `schema:{version}:names`  -- `list[str]` of table names (JSON), in the
    same row order as `:index`'s matrix
  - `schema:{version}:index`  -- the embeddings matrix, `np.save`d into a
    `BytesIO` buffer

All three keys must be present, and the stored graph's own `version` field
must match the requested `version`, for `load()` to report a hit. Anything
else -- a missing part, a version mismatch, corrupt bytes -- is a miss, not
an exception, matching `RedisCache`'s fail-open design (see
`app/cache/redis.py`).

One more `schema:` key lives here but is *not* a cache entry: `schema:epoch`
(`app.cache.keys.SCHEMA_EPOCH_KEY`), the opaque token every process polls to
learn that some *other* process re-introspected -- see
`app.services.schema_sync`. `get_epoch()`/`bump_epoch()` own it, and
`invalidate_all()` deliberately preserves it.
"""

from __future__ import annotations

import io
import uuid

import numpy as np
import structlog
from numpy.typing import NDArray

from app.cache.keys import SCHEMA_EPOCH_KEY, schema_cache_key
from app.cache.redis import RedisCache
from app.core.schema.models import SchemaGraph

__all__ = ["SchemaCache"]

logger = structlog.get_logger(__name__)


class SchemaCache:
    def __init__(self, cache: RedisCache, *, ttl_s: int) -> None:
        self._cache = cache
        self._ttl_s = ttl_s

    async def load(self, version: str) -> tuple[SchemaGraph, list[str], NDArray[np.float64]] | None:
        base = schema_cache_key(version)
        graph_raw = await self._cache.get_bytes(f"{base}:graph")
        names = await self._cache.get_json(f"{base}:names")
        index_raw = await self._cache.get_bytes(f"{base}:index")
        if graph_raw is None or names is None or index_raw is None:
            return None
        try:
            graph = SchemaGraph.model_validate_json(graph_raw)
            matrix = np.load(io.BytesIO(index_raw))
        except (ValueError, TypeError, EOFError) as exc:
            # A corrupt/foreign value under our keys shouldn't raise -- treat
            # it the same as a miss (matches RedisCache's own philosophy).
            logger.warning("schema_cache_corrupt", version=version, error=str(exc))
            return None
        if graph.version != version:
            return None
        return graph, list(names), matrix

    async def store(
        self, graph: SchemaGraph, names: list[str], matrix: NDArray[np.float64]
    ) -> None:
        base = schema_cache_key(graph.version)
        buf = io.BytesIO()
        np.save(buf, matrix)
        await self._cache.set_bytes(
            f"{base}:graph", graph.model_dump_json().encode(), ttl_s=self._ttl_s
        )
        await self._cache.set_json(f"{base}:names", names, ttl_s=self._ttl_s)
        await self._cache.set_bytes(f"{base}:index", buf.getvalue(), ttl_s=self._ttl_s)

    async def get_epoch(self) -> str | None:
        """The current schema epoch (an opaque token written by
        `bump_epoch`), or `None` if none was ever written or Redis is
        unavailable/disabled."""
        value = await self._cache.get_json(SCHEMA_EPOCH_KEY)
        # Anything that isn't a string is a foreign/corrupt value under our
        # key -- report "no epoch" rather than handing a watcher something
        # it would compare against a `str | None`.
        return value if isinstance(value, str) else None

    async def bump_epoch(self) -> str | None:
        """Write a fresh epoch token (uuid4 hex) and return it, or `None` if
        the write failed (Redis down/disabled) -- the caller keeps its own
        components; other processes simply will not learn about this
        refresh."""
        token = uuid.uuid4().hex
        stored = await self._cache.set_json(SCHEMA_EPOCH_KEY, token, ttl_s=self._ttl_s)
        return token if stored else None

    async def invalidate_all(self) -> int:
        """Drop every cached schema entry, returning how many were removed.

        `schema:epoch` shares the prefix this sweeps but is not a cache
        entry -- it is the cross-process refresh token -- so it is read
        first, written back after, and left out of the count. The gap in
        between reads as "no epoch yet", which `sync_schema_if_stale`
        treats as a no-op rather than as a change, so a watcher polling
        mid-sweep cannot be tricked into a spurious refresh; and the callers
        that do reach here -- the admin-refresh path, `POST
        /api/v1/schema/refresh` and `refresh_schema_job` -- bump the epoch
        immediately after anyway. The watcher path never invalidates: it
        calls `refresh_components(..., invalidate=False)` so that it loads
        the index the refreshing process just stored.
        """
        epoch = await self.get_epoch()
        deleted = await self._cache.delete_prefix("schema:")
        if epoch is None:
            return deleted
        await self._cache.set_json(SCHEMA_EPOCH_KEY, epoch, ttl_s=self._ttl_s)
        return max(deleted - 1, 0)
