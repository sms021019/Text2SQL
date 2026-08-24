"""`QueryCache`: the app-side implementation of
`app.core.pipeline.SqlCache`, backed by `RedisCache`.

One cache key (see `app.cache.keys.sql_cache_key`) is shared by two Redis
entries: the SQL tier (`sql:{hash}`) holds the guard-passed, possibly
repaired SQL plus the tables it referenced (so a pipeline SQL-tier hit can
re-run the guard without re-retrieving them); the result tier (`res:{hash}`,
derived via `result_cache_key`) additionally holds a full serialised
`QueryResult` (`QueryResult.to_dict()`), opt-in via `use_result_cache`.

`schema_version`/`model`/`prompt_version` are baked into key derivation at
construction time -- a schema refresh, a model change, or a prompt-version
bump each need a fresh `QueryCache` (and, in turn, a fresh
`Text2SQLPipeline` pointed at it), so both `app.main`'s lifespan and
`POST /api/v1/schema/refresh` build both together via
`app.services.schema_service.build_pipeline` rather than constructing a
`QueryCache` directly.
"""

from __future__ import annotations

from app.cache.keys import result_cache_key, sql_cache_key
from app.cache.redis import RedisCache
from app.core.sql.executor import QueryResult

__all__ = ["QueryCache"]


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
    ) -> None:
        self._redis = redis
        self._schema_version = schema_version
        self._model = model
        self._prompt_version = prompt_version
        self._sql_ttl_s = sql_ttl_s
        self._result_ttl_s = result_ttl_s

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
        # `RedisCache` has no single-key delete; `key` is a fixed-length
        # (32 hex char) sha256-derived digest, so treating it as a SCAN
        # prefix is, in practice, exact -- no other key can share it as a
        # proper prefix.
        await self._redis.delete_prefix(key)

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
        await self._redis.set_json(
            result_cache_key(sql_key=key),
            {
                "sql": sql,
                "explanation": explanation,
                "tables": tables,
                "result": result.to_dict(),
            },
            ttl_s=self._result_ttl_s,
        )
