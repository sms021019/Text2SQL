"""FastAPI dependency providers, all reading off `app.state` (populated by
`create_app()`'s lifespan -- see `app/main.py`).
"""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.pipeline import Text2SQLPipeline
from app.core.schema.models import SchemaGraph

__all__ = ["get_graph", "get_pipeline", "get_session_factory"]


def _require_ready(request: Request) -> None:
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status_code=503, detail="schema not loaded")


def get_pipeline(request: Request) -> Text2SQLPipeline:
    _require_ready(request)
    return cast(Text2SQLPipeline, request.app.state.pipeline)


def get_graph(request: Request) -> SchemaGraph:
    _require_ready(request)
    return cast(SchemaGraph, request.app.state.graph)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_factory)
