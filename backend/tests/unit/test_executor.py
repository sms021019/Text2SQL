"""Unit test for `QueryResult.to_dict`/`from_dict` -- pure serialisation,
no database involved, so it lives in `unit/` (see `execute_readonly`'s
integration coverage elsewhere for the database-touching behaviour)."""

from __future__ import annotations

import json

from app.core.sql.executor import QueryResult


def test_to_dict_from_dict_roundtrip() -> None:
    result = QueryResult(
        columns=["id", "name"],
        rows=[[1, "alice"], [2, "bob"]],
        row_count=2,
        duration_ms=12.5,
        truncated=False,
    )

    restored = QueryResult.from_dict(result.to_dict())

    assert restored == result


def test_to_dict_is_json_serialisable() -> None:
    result = QueryResult(columns=["n"], rows=[[42]], row_count=1, duration_ms=1.0, truncated=True)

    # Round-trips through actual JSON text, matching how the result cache
    # (`app.cache.query_cache.QueryCache`, via `RedisCache.set_json`) uses it.
    restored = QueryResult.from_dict(json.loads(json.dumps(result.to_dict())))

    assert restored == result


def test_from_dict_missing_key_raises_key_error() -> None:
    try:
        QueryResult.from_dict({"columns": ["id"]})
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")
