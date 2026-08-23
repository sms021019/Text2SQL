import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.sql.executor import ExecutionError, execute_readonly

pytestmark = pytest.mark.integration


async def test_selects_rows_from_customers(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        result = await execute_readonly(
            engine,
            "SELECT id, email FROM customers ORDER BY id",
            statement_timeout_ms=5000,
            max_rows=10000,
        )
    finally:
        await engine.dispose()

    assert result.columns == ["id", "email"]
    assert result.row_count > 0
    assert result.row_count == len(result.rows)
    assert result.truncated is False
    assert result.duration_ms >= 0


async def test_max_rows_applied_with_truncated_flag(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        result = await execute_readonly(
            engine, "SELECT id FROM customers ORDER BY id", statement_timeout_ms=5000, max_rows=3
        )
    finally:
        await engine.dispose()

    assert result.row_count == 3
    assert len(result.rows) == 3
    assert result.truncated is True


async def test_pg_sleep_raises_timeout_error(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        with pytest.raises(ExecutionError) as exc_info:
            await execute_readonly(
                engine, "SELECT pg_sleep(10)", statement_timeout_ms=200, max_rows=10
            )
    finally:
        await engine.dispose()

    assert exc_info.value.kind == "timeout"
    assert "statement timeout" in exc_info.value.pg_message.lower()
    assert str(exc_info.value) == f"timeout: {exc_info.value.pg_message}"


async def test_insert_raises_permission_error(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        with pytest.raises(ExecutionError) as exc_info:
            await execute_readonly(
                engine,
                "INSERT INTO customers (first_name) VALUES ('x')",
                statement_timeout_ms=5000,
                max_rows=10,
            )
    finally:
        await engine.dispose()

    assert exc_info.value.kind == "permission"


async def test_bad_syntax_raises_syntax_error(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        with pytest.raises(ExecutionError) as exc_info:
            await execute_readonly(
                engine, "SELEKT * FROM customers", statement_timeout_ms=5000, max_rows=10
            )
    finally:
        await engine.dispose()

    assert exc_info.value.kind == "syntax"


async def test_result_rows_are_json_serialisable(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        result = await execute_readonly(
            engine,
            "SELECT c.signup_date, c.created_at, p.unit_price "
            "FROM customers c CROSS JOIN products p LIMIT 5",
            statement_timeout_ms=5000,
            max_rows=100,
        )
    finally:
        await engine.dispose()

    assert result.row_count == 5
    json.dumps(result.rows)  # must not raise
    signup_date, created_at, unit_price = result.rows[0]
    assert isinstance(signup_date, str)
    assert isinstance(created_at, str)
    assert isinstance(unit_price, float)
