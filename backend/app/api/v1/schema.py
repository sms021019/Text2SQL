"""`GET /schema` (current `SchemaGraph`, rendered as JSON) and
`POST /schema/refresh` (re-introspect the target DB, rebuild the retriever
index, and swap in a fresh `Text2SQLPipeline`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import _require_ready, get_graph
from app.core.schema.models import SchemaGraph
from app.services.bootstrap import publish_components, refresh_components
from app.services.schema_sync import mark_refreshed

__all__ = ["router"]

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
    """Rebuild this app's schema-dependent runtime in place, announce the
    refresh to every other process, and re-publish onto `app.state`.

    The rebuild itself lives in `app.services.bootstrap.refresh_components`,
    shared with the worker-side `app.jobs.tasks.refresh_schema_job`;
    `mark_refreshed` then bumps the shared Redis epoch so the arq worker and
    any other API replica re-introspect on their next poll (see
    `app.services.schema_sync`); `publish_components` swaps the `app.state`
    mirror the request handlers read -- with no `await` between the rebuild
    and that swap other than the epoch write, whose failure mode is a
    no-op."""
    state = request.app.state

    graph = await refresh_components(state.components)
    await mark_refreshed(state.components)
    publish_components(state, state.components)

    return RefreshResponse(version=graph.version)
