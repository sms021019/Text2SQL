"""FastAPI application factory and the module-level `app` uvicorn serves.

`create_app()` wires up the whole request-time stack in its lifespan:
migrate the app DB, open both engines, build (or accept an injected) LLM
client, introspect the target schema, build the retrieval index, and
assemble a `Text2SQLPipeline` -- all stored on `app.state` for the route
handlers in `app/api/v1/**` to read via `app/api/deps.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.router import router
from app.config import Settings, get_settings
from app.core.pipeline import Text2SQLPipeline
from app.core.prompting.builder import PromptBuilder
from app.core.schema.introspect import introspect
from app.core.schema.retrieve import SchemaRetriever
from app.db.migrate import upgrade_to_head
from app.db.session import make_engine
from app.llm.base import LLMClient
from app.llm.factory import build_llm
from app.observability.logging import configure_logging
from app.observability.middleware import RequestIDMiddleware

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)


def _load_examples(path: str) -> list[dict[str, Any]]:
    """Load few-shot examples for `PromptBuilder` from `path`.

    Tolerates a missing file (returns no examples, with a warning) -- see
    `Settings.examples_path`'s docstring for why the file may not be there.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("examples file not found, continuing with no examples: %s", p)
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not data:
        return []
    return list(data)


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    """Build the FastAPI app. `settings`/`llm` are injectable so tests can
    point at a container database and a `FakeLLM` instead of the real
    services `get_settings()`/`build_llm()` would otherwise resolve."""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Configured *after* the migration, not before: Alembic's env.py
        # calls `logging.config.fileConfig(alembic.ini)`, which replaces the
        # root logger's handlers with alembic.ini's plain-text ones -- an
        # earlier `configure_logging()` call here would just get clobbered
        # by that, silently reverting every log line for the rest of the
        # process back out of JSON.
        await asyncio.to_thread(upgrade_to_head, resolved_settings.app_db_url)

        configure_logging(resolved_settings.log_level)

        target_engine = make_engine(resolved_settings.target_db_url)
        app_engine = make_engine(resolved_settings.app_db_url)
        session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

        app_llm = llm or build_llm(resolved_settings)

        graph = await introspect(target_engine)
        retriever = SchemaRetriever(graph, app_llm, top_k=resolved_settings.retrieve_top_k)
        try:
            await asyncio.wait_for(
                retriever.build_index(), timeout=resolved_settings.startup_embed_timeout_s
            )
        except TimeoutError:
            logger.warning(
                "schema index build timed out after %ss, will retry lazily",
                resolved_settings.startup_embed_timeout_s,
            )
            retriever.mark_build_failed(
                f"embedding timed out during startup after "
                f"{resolved_settings.startup_embed_timeout_s}s"
            )

        examples = _load_examples(resolved_settings.examples_path)
        builder = PromptBuilder(resolved_settings.prompt_version, examples)

        pipeline = Text2SQLPipeline(
            llm=app_llm,
            retriever=retriever,
            graph=graph,
            builder=builder,
            target_engine=target_engine,
            settings=resolved_settings,
        )

        app.state.settings = resolved_settings
        app.state.target_engine = target_engine
        app.state.app_engine = app_engine
        app.state.session_factory = session_factory
        app.state.llm = app_llm
        app.state.graph = graph
        app.state.retriever = retriever
        app.state.builder = builder
        app.state.pipeline = pipeline
        app.state.ready = True

        try:
            yield
        finally:
            app.state.ready = False
            await target_engine.dispose()
            await app_engine.dispose()
            aclose = getattr(app_llm, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="Text2SQL API", lifespan=lifespan)
    app.state.ready = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)

    app.include_router(router)
    return app


app = create_app()
