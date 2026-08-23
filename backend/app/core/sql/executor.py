"""Read-only, timeout-bounded execution of a single SQL statement.

`execute_readonly` is the last line of defence before a query touches the
database (see the "Security boundary" note in `guard.py`): every connection
it opens is put into `SET TRANSACTION READ ONLY` and given a per-transaction
`statement_timeout` *by this module itself*, regardless of what the
connecting role's own defaults are -- the executor may be pointed at
databases/roles other than the project's `readonly` role, so it cannot rely
on server-side defaults alone.

`SET` does not accept bind parameters, so the timeout is inlined into the
SQL text; `int(statement_timeout_ms)` is the sanitisation that makes that
safe (a non-numeric value raises `TypeError`/`ValueError` before it ever
reaches the database).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.errors import DomainError

__all__ = ["ExecutionError", "QueryResult", "execute_readonly"]

ErrorKind = Literal["syntax", "timeout", "permission", "other"]

#: SQLSTATEs mapped to `ExecutionError.kind`. See
#: https://www.postgresql.org/docs/current/errcodes-appendix.html
_TIMEOUT_SQLSTATES = frozenset({"57014"})  # query_canceled (statement_timeout)
_SYNTAX_SQLSTATES = frozenset(
    {
        "42601",  # syntax_error
        "42P01",  # undefined_table
        "42703",  # undefined_column
        "42883",  # undefined_function
        "42P10",  # invalid_column_reference
    }
)
#: read_only_sql_transaction, insufficient_privilege
_PERMISSION_SQLSTATES = frozenset({"25006", "42501"})


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    duration_ms: float
    truncated: bool


class ExecutionError(DomainError):
    """A read-only query failed against the database.

    `kind` is a stable, machine-readable category derived from the
    underlying asyncpg SQLSTATE (see `_kind_for`); `pg_message` is
    the driver's message, first line only.
    """

    def __init__(self, kind: ErrorKind, pg_message: str) -> None:
        self.kind = kind
        self.pg_message = pg_message
        super().__init__(f"{kind}: {pg_message}")


async def execute_readonly(
    engine: AsyncEngine, sql: str, *, statement_timeout_ms: int, max_rows: int
) -> QueryResult:
    """Run `sql` read-only against `engine`, capped at `max_rows` rows.

    Opens one connection, forces the transaction read-only and bounds it
    with `statement_timeout_ms`, executes `sql`, and fetches at most
    `max_rows + 1` rows to detect truncation without pulling an unbounded
    result set. The transaction is always rolled back -- this executor never
    commits.

    Raises:
        ExecutionError: If the database rejects the statement or the
            timeout fires; `kind` classifies the failure by SQLSTATE.
    """
    async with engine.connect() as conn:
        try:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))

            start = time.perf_counter()
            result = await conn.execute(text(sql))
            columns = list(result.keys())
            rows = result.fetchmany(max_rows + 1)
            duration_ms = (time.perf_counter() - start) * 1000
        except DBAPIError as exc:
            raise ExecutionError(_kind_for(exc), _pg_message(exc)) from exc
        finally:
            await conn.rollback()

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return QueryResult(
        columns=columns,
        rows=[[_coerce(value) for value in row] for row in rows],
        row_count=len(rows),
        duration_ms=duration_ms,
        truncated=truncated,
    )


def _kind_for(exc: DBAPIError) -> ErrorKind:
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in _TIMEOUT_SQLSTATES:
        return "timeout"
    if sqlstate in _SYNTAX_SQLSTATES:
        return "syntax"
    if sqlstate in _PERMISSION_SQLSTATES:
        return "permission"
    return "other"


def _pg_message(exc: DBAPIError) -> str:
    message = str(exc.orig) if exc.orig is not None else str(exc)
    first_line, _, _ = message.partition("\n")
    return first_line


def _coerce(value: Any) -> Any:
    """Coerce one cell into a JSON-serialisable value.

    Anything not covered here (str, int, float, bool, None, ...) already is
    JSON-serialisable and passes through unchanged.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | time_):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, timedelta):
        return str(value)
    return value
