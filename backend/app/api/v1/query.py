"""`POST /query`: run the pipeline for one question and log the result.

Always returns 200 (even a pipeline error surfaces in the `error` field --
see `Text2SQLPipeline.run`'s docstring), except for a 422 on an
empty/too-long `question` (plain pydantic validation) or a 503 if the
schema has not finished loading (`get_pipeline` dependency).

`QueryRequest`/`QueryResponse` and the `PipelineOutput` -> response mapping
live in `app.services.query_service`, shared with the async counterpart
(`POST /api/v1/query/async`, `app/api/v1/jobs.py`) -- re-exported here so
existing imports of these names from this module keep working.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_graph, get_pipeline, get_session_factory
from app.core.pipeline import Text2SQLPipeline
from app.core.schema.models import SchemaGraph
from app.services.query_service import (
    QueryRequest,
    QueryResponse,
    TimingOut,
    UsageOut,
    persist_query_log,
    to_query_response,
)

__all__ = ["QueryRequest", "QueryResponse", "TimingOut", "UsageOut", "router"]

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def run_query(
    body: QueryRequest,
    request: Request,
    pipeline: Text2SQLPipeline = Depends(get_pipeline),
    graph: SchemaGraph = Depends(get_graph),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> QueryResponse:
    # `graph` is a dependency rather than a `state.graph` read after the run:
    # the schema-epoch watcher can swap `app.state` while the pipeline is
    # awaiting, and this row must record the version the answer came from.
    out = await pipeline.run(
        body.question, use_cache=body.use_cache, use_result_cache=body.use_result_cache
    )
    request_id: str = request.state.request_id
    state = request.app.state

    await persist_query_log(
        session_factory,
        out,
        question=body.question,
        model=out.model or state.settings.llm_model,
        schema_version=graph.version,
        request_id=request_id,
    )

    return to_query_response(out, request_id=request_id)
