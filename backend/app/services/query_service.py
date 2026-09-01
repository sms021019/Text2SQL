"""`QueryRequest`/`QueryResponse` (and their nested `TimingOut`/`UsageOut`)
plus the `PipelineOutput` -> response/DB-row plumbing shared by the sync
`POST /api/v1/query` route (`app/api/v1/query.py`) and the async
`run_query_job` arq task (`app/jobs/tasks.py`), so both go through exactly
one place rather than two copies of the same field-by-field mapping drifting
apart.

`to_query_response()` is pure (no I/O); `persist_query_log()` opens its own
session from the factory it is handed and writes the `QueryLog` row via
`app.db.query_log.record_query`, swallowing (and logging) any failure --
session open and close included -- so a logging problem never fails the
request/job itself, the same tolerance `POST /api/v1/query` already had
before this module existed.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.pipeline import CacheStatus, PipelineOutput
from app.db.query_log import record_query

__all__ = [
    "QueryRequest",
    "QueryResponse",
    "TimingOut",
    "UsageOut",
    "persist_query_log",
    "to_query_response",
]

logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    use_cache: bool = True
    use_result_cache: bool = False


class TimingOut(BaseModel):
    stage: str
    ms: float


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


class QueryResponse(BaseModel):
    sql: str
    explanation: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    tables: list[str]
    repaired: bool
    timings: list[TimingOut]
    usage: UsageOut
    error: str | None
    request_id: str
    cache_status: CacheStatus


def to_query_response(out: PipelineOutput, *, request_id: str) -> QueryResponse:
    """Map one `Text2SQLPipeline.run()` result onto the `QueryResponse`
    shape returned by both `POST /api/v1/query` and, as a `.model_dump()`
    dict (arq pickles job results), `run_query_job`."""
    result = out.result
    return QueryResponse(
        sql=out.sql,
        explanation=out.explanation,
        columns=result.columns if result is not None else [],
        rows=result.rows if result is not None else [],
        row_count=result.row_count if result is not None else 0,
        truncated=result.truncated if result is not None else False,
        tables=out.tables,
        repaired=out.repaired,
        timings=[TimingOut(stage=t.stage, ms=t.ms) for t in out.timings],
        usage=UsageOut(
            prompt_tokens=out.usage.prompt_tokens,
            completion_tokens=out.usage.completion_tokens,
            latency_ms=out.usage.latency_ms,
        ),
        error=out.error,
        request_id=request_id,
        cache_status=out.cache_status,
    )


async def persist_query_log(
    session_factory: async_sessionmaker[AsyncSession],
    out: PipelineOutput,
    *,
    question: str,
    model: str,
    schema_version: str,
    request_id: str,
) -> None:
    """Persist `out` as a `QueryLog` row, tolerating (and logging) any
    failure -- a logging problem must never fail the request/job that
    produced `out`.

    Takes the *session factory*, not a session, so that opening and closing
    the session happen inside this `try` too. A failed `commit()` leaves the
    session rollback-required, and the `async with`'s own `__aexit__` can
    then raise a second exception on close; if the caller owned the `async
    with`, that second one would escape past this handler and turn an
    app-database outage into a 500 (or a `status="failed"` job).
    """
    try:
        async with session_factory() as session:
            await record_query(
                session,
                out,
                question=question,
                model=model,
                schema_version=schema_version,
                request_id=request_id,
            )
    except Exception:  # noqa: BLE001 -- a logging failure must not fail the caller
        logger.exception("failed to record query_log", extra={"request_id": request_id})
