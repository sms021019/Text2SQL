"""`QueryCache`: the app-side implementation of
`app.core.pipeline.SqlCache`, backed by `RedisCache`.

One cache key (see `app.cache.keys.sql_cache_key`) is shared by two Redis
entries: the SQL tier (`sql:{hash}`) holds the guard-passed, possibly
repaired SQL plus the tables it referenced (so a pipeline SQL-tier hit can
re-run the guard without re-retrieving them); the result tier (`res:{hash}`,
derived via `result_cache_key`) additionally holds a full serialised
`QueryResult` (`QueryResult.to_dict()`), opt-in via `use_result_cache`.

A result-tier write larger than `result_max_bytes` (default 1 MiB, from
`Settings.result_cache_max_bytes`) is skipped entirely (logged at debug) --
see `set_result` -- so one huge row set can't crowd everything else out of
Redis under memory pressure.

`schema_version`/`model`/`prompt_version` are baked into key derivation at
construction time -- a schema refresh, a model change, or a prompt-version
bump each need a fresh `QueryCache` (and, in turn, a fresh
`Text2SQLPipeline` pointed at it), so both `app.main`'s lifespan and
`POST /api/v1/schema/refresh` build both together via
`app.services.schema_service.build_pipeline` rather than constructing a
`QueryCache` directly.
"""

from __future__ import annotations

import json

import structlog

from app.cache.keys import result_cache_key, sql_cache_key
from app.cache.redis import RedisCache
from app.core.sql.executor import QueryResult

__all__ = ["QueryCache"]

logger = structlog.get_logger(__name__)


class QueryCache:
    def __init__(
        self,
        redis: RedisCache,
        *,
        schema_version: str,
        model: str,
        prompt_version: str,
        sql_ttl_s: int,
        result_ttl_s: int,
        result_max_bytes: int = 1_048_576,
    ) -> None:
        self._redis = redis
        self._schema_version = schema_version
        self._model = model
        self._prompt_version = prompt_version
        self._sql_ttl_s = sql_ttl_s
        self._result_ttl_s = result_ttl_s
        self._result_max_bytes = result_max_bytes

    def key_for(self, question: str) -> str:
        return sql_cache_key(
            schema_version=self._schema_version,
            model=self._model,
            prompt_version=self._prompt_version,
            question=question,
        )

    async def get_sql(self, key: str) -> tuple[str, str, list[str]] | None:
        data = await self._redis.get_json(key)
        if not isinstance(data, dict):
            return None
        try:
            return str(data["sql"]), str(data["explanation"]), [str(t) for t in data["tables"]]
        except (KeyError, TypeError):
            return None

    async def set_sql(self, key: str, sql: str, explanation: str, tables: list[str]) -> None:
        await self._redis.set_json(
            key,
            {"sql": sql, "explanation": explanation, "tables": tables},
            ttl_s=self._sql_ttl_s,
        )

    async def delete_sql(self, key: str) -> None:
        # Evict both tiers together -- a stale SQL-tier entry's result-tier
        # sibling (if any) is just as stale, and leaving it behind would let
        # a later `use_result_cache=True` call resurrect the very answer
        # this eviction was meant to invalidate.
        await self._redis.delete(key, result_cache_key(sql_key=key))

    async def get_result(self, key: str) -> tuple[str, str, list[str], QueryResult] | None:
        data = await self._redis.get_json(result_cache_key(sql_key=key))
        if not isinstance(data, dict):
            return None
        try:
            result = QueryResult.from_dict(data["result"])
            tables = [str(t) for t in data["tables"]]
            return str(data["sql"]), str(data["explanation"]), tables, result
        except (KeyError, TypeError, ValueError):
            return None

    async def set_result(
        self, key: str, sql: str, explanation: str, tables: list[str], result: QueryResult
    ) -> None:
        payload = {
            "sql": sql,
            "explanation": explanation,
            "tables": tables,
            "result": result.to_dict(),
        }
        try:
            size = len(json.dumps(payload).encode())
        except (TypeError, ValueError):
            size = 0  # let `RedisCache.set_json`'s own error handling below deal with it
        if size > self._result_max_bytes:
            logger.debug(
                "result_cache_skipped_oversized",
                key=key,
                size_bytes=size,
                max_bytes=self._result_max_bytes,
            )
            return
        await self._redis.set_json(
            result_cache_key(sql_key=key), payload, ttl_s=self._result_ttl_s
        )
