"""`GET /schema` (current `SchemaGraph`, rendered as JSON) and
`POST /schema/refresh` (re-introspect the target DB, rebuild the retriever
index, and swap in a fresh `Text2SQLPipeline`).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import _require_ready, get_graph
from app.core.pipeline import Text2SQLPipeline
from app.core.schema.introspect import introspect
from app.core.schema.models import SchemaGraph
from app.core.schema.retrieve import SchemaRetriever

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    comment: str | None
    is_pk: bool


class TableOut(BaseModel):
    name: str
    comment: str | None
    columns: list[ColumnOut]


class SchemaResponse(BaseModel):
    version: str
    tables: list[TableOut]


class RefreshResponse(BaseModel):
    version: str


@router.get("/schema", response_model=SchemaResponse)
async def get_schema(graph: SchemaGraph = Depends(get_graph)) -> SchemaResponse:
    tables = [
        TableOut(
            name=table.name,
            comment=table.comment,
            columns=[
                ColumnOut(
                    name=col.name,
                    type=col.type,
                    nullable=col.nullable,
                    comment=col.comment,
                    is_pk=col.is_pk,
                )
                for col in table.columns
            ],
        )
        for table in graph.tables.values()
    ]
    tables.sort(key=lambda t: t.name)
    return SchemaResponse(version=graph.version, tables=tables)


@router.post(
    "/schema/refresh",
    response_model=RefreshResponse,
    dependencies=[Depends(_require_ready)],
)
async def refresh_schema(request: Request) -> RefreshResponse:
    state = request.app.state

    graph = await introspect(state.target_engine)
    retriever = SchemaRetriever(graph, state.llm, top_k=state.settings.retrieve_top_k)
    try:
        await asyncio.wait_for(
            retriever.build_index(), timeout=state.settings.startup_embed_timeout_s
        )
    except TimeoutError:
        logger.warning(
            "schema index rebuild timed out after %ss, will retry lazily",
            state.settings.startup_embed_timeout_s,
        )
        retriever.mark_build_failed(
            f"embedding timed out during refresh after {state.settings.startup_embed_timeout_s}s"
        )
    pipeline = Text2SQLPipeline(
        llm=state.llm,
        retriever=retriever,
        graph=graph,
        builder=state.builder,
        target_engine=state.target_engine,
        settings=state.settings,
    )

    # No `await` between here and the assignments above, so this swap is
    # atomic with respect to any other request task on the same event loop.
    state.graph = graph
    state.retriever = retriever
    state.pipeline = pipeline

    return RefreshResponse(version=graph.version)
