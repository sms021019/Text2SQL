"""`POST /query`: run the pipeline for one question and log the result.

Always returns 200 (even a pipeline error surfaces in the `error` field --
see `Text2SQLPipeline.run`'s docstring), except for a 422 on an
empty/too-long `question` (plain pydantic validation) or a 503 if the
schema has not finished loading (`get_pipeline` dependency).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_pipeline, get_session_factory
from app.core.pipeline import Text2SQLPipeline
from app.db.query_log import record_query

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()


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
    cache_status: str


@router.post("/query", response_model=QueryResponse)
async def run_query(
    body: QueryRequest,
    request: Request,
    pipeline: Text2SQLPipeline = Depends(get_pipeline),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> QueryResponse:
    out = await pipeline.run(
        body.question, use_cache=body.use_cache, use_result_cache=body.use_result_cache
    )
    request_id: str = request.state.request_id

    try:
        state = request.app.state
        async with session_factory() as session:
            await record_query(
                session,
                out,
                question=body.question,
                model=out.model or state.settings.llm_model,
                schema_version=state.graph.version,
                request_id=request_id,
            )
    except Exception:  # noqa: BLE001 -- a logging failure must not fail the request
        logger.exception("failed to record query_log", extra={"request_id": request_id})

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
