"""Writes one `QueryLog` row per pipeline run."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pipeline import PipelineOutput
from app.db.models import QueryLog

__all__ = ["record_query"]


async def record_query(
    session: AsyncSession,
    out: PipelineOutput,
    *,
    question: str,
    model: str,
    schema_version: str,
    request_id: str,
) -> QueryLog:
    """Persist `out` (one `Text2SQLPipeline.run()` result) as a `QueryLog`
    row, committing it, and return the row.

    `success` is derived from `out.error is None`; `latency_ms` sums every
    stage timing (`out.timings`), i.e. wall time across the whole pipeline
    run rather than just LLM latency (`out.usage.latency_ms`).

    `cache_status` is copied from `out.cache_status` so cache outcomes are
    queryable historically (ADR 0003).
    """
    row = QueryLog(
        question=question,
        sql=out.sql,
        tables=out.tables,
        success=out.error is None,
        error=out.error,
        repaired=out.repaired,
        latency_ms=sum(t.ms for t in out.timings),
        prompt_tokens=out.usage.prompt_tokens,
        completion_tokens=out.usage.completion_tokens,
        model=model,
        schema_version=schema_version,
        request_id=request_id,
        cache_status=out.cache_status,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    # `commit()` expires every attribute by default, so a caller reading
    # `row.id` etc. right after this returns would otherwise trigger a
    # lazy load outside of any `await` -- refresh eagerly instead.
    await session.refresh(row)
    return row
