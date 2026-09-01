"""The arq task functions the worker runs.

Both read the `AppComponents` the worker put in `ctx["components"]` (see
`app.jobs.worker`), so a job goes through exactly the same pipeline, caches,
and schema version the synchronous routes do.

`run_query_job` deliberately does not raise on a *pipeline* error (a guard
rejection, a failed statement, an LLM outage): `Text2SQLPipeline.run()`
reports those in `PipelineOutput.error`, so the job completes normally with
the error inside its result -- byte-for-byte the same `QueryResponse` shape
`POST /api/v1/query` returns. Only a genuine bug (an unexpected exception)
makes the job itself fail, which `GET /api/v1/jobs/{job_id}` then reports as
`status="failed"`.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.bootstrap import AppComponents, refresh_components
from app.services.query_service import persist_query_log, to_query_response

__all__ = ["refresh_schema_job", "run_query_job"]

logger = logging.getLogger(__name__)


def _components(ctx: dict[Any, Any]) -> AppComponents:
    """The runtime `app.jobs.worker`'s `on_startup` seeded into `ctx`."""
    components = ctx.get("components")
    if not isinstance(components, AppComponents):
        raise RuntimeError("worker ctx has no AppComponents; on_startup did not run")
    return components


async def run_query_job(
    ctx: dict[Any, Any],
    question: str,
    *,
    use_cache: bool = True,
    use_result_cache: bool = False,
    request_id: str,
) -> dict[str, Any]:
    """Run one question through the pipeline and return the `QueryResponse`
    as a plain dict (arq pickles job results, so a pydantic model would tie
    every reader to this exact class), logging a `query_log` row on the way
    -- the same two steps `POST /api/v1/query` performs inline."""
    components = _components(ctx)

    out = await components.pipeline.run(
        question, use_cache=use_cache, use_result_cache=use_result_cache
    )

    async with components.session_factory() as session:
        await persist_query_log(
            session,
            out,
            question=question,
            model=out.model or components.settings.llm_model,
            schema_version=components.graph.version,
            request_id=request_id,
        )

    logger.info(
        "query_job_done",
        extra={"request_id": request_id, "ok": out.error is None},
    )
    return to_query_response(out, request_id=request_id).model_dump()


async def refresh_schema_job(ctx: dict[Any, Any]) -> dict[str, str]:
    """Re-introspect the target database and rebuild this worker's retriever
    and pipeline, returning the new schema version -- the worker-side twin of
    `POST /api/v1/schema/refresh`, both going through
    `app.services.bootstrap.refresh_components`."""
    components = _components(ctx)
    graph = await refresh_components(components)
    return {"version": graph.version}
