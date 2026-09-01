"""Async counterpart to `POST /api/v1/query`: enqueue a question onto the
arq queue (`POST /api/v1/query/async`) and poll for its outcome
(`GET /api/v1/jobs/{job_id}`).

The request body is the very same `QueryRequest` the synchronous route
takes, and a finished job's `result` is the very same `QueryResponse` -- one
schema for both, so a client can switch between them without remapping
anything (see `app.services.query_service`).

Both routes need the `ArqRedis` pool `app.main`'s lifespan puts on
`app.state.arq_pool`. With Redis unreachable that pool is `None` and both
return 503: background work degrades, while the synchronous `/query` path
keeps answering (the Phase 2 plan's "cache is transparent" constraint).
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from redis.exceptions import RedisError

from app.services.query_service import QueryRequest, QueryResponse

__all__ = ["EnqueueResponse", "JobStatusResponse", "router"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: What `GET /jobs/{job_id}` reports. arq's `deferred` (queued, but not yet
#: due) is reported as `queued` -- nothing here ever defers a job, and the
#: distinction is meaningless to a caller that just wants its answer.
JobState = Literal["queued", "in_progress", "complete", "failed", "not_found"]

_STATUS_MAP: dict[JobStatus, JobState] = {
    JobStatus.deferred: "queued",
    JobStatus.queued: "queued",
    JobStatus.in_progress: "in_progress",
    JobStatus.complete: "complete",
    JobStatus.not_found: "not_found",
}

_UNAVAILABLE = "job queue unavailable"


class EnqueueResponse(BaseModel):
    job_id: str
    status: JobState


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobState
    #: The finished job's answer -- `None` until it completes, and also for a
    #: job that failed (the exception is in `error` instead).
    result: QueryResponse | None = None
    #: Set only when the task function itself raised, i.e. a bug in the
    #: worker. A *pipeline* error (a guard rejection, a failed statement, an
    #: LLM outage) is not a job failure: the job completes and the message
    #: lands in `result.error`, exactly as `POST /query` reports it.
    error: str | None = None


def _pool(request: Request) -> ArqRedis:
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    return cast(ArqRedis, pool)


@router.post("/query/async", response_model=EnqueueResponse, status_code=202)
async def enqueue_query(body: QueryRequest, request: Request) -> EnqueueResponse:
    pool = _pool(request)
    request_id: str = request.state.request_id

    try:
        job = await pool.enqueue_job(
            "run_query_job",
            body.question,
            use_cache=body.use_cache,
            use_result_cache=body.use_result_cache,
            request_id=request_id,
        )
    except (RedisError, OSError) as exc:
        # The pool was alive at startup but Redis has gone away since.
        logger.warning("enqueue failed: %s", exc, extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc

    if job is None:  # pragma: no cover -- only when a job id is reused
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)

    return EnqueueResponse(job_id=job.job_id, status="queued")


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str, request: Request) -> JobStatusResponse:
    pool = _pool(request)
    job = Job(job_id, redis=pool)

    try:
        status = _STATUS_MAP[await job.status()]
        if status != "complete":
            return JobStatusResponse(job_id=job_id, status=status)
        info = await job.result_info()
    except (RedisError, OSError) as exc:
        logger.warning("job lookup failed: %s", exc, extra={"request_id": request.state.request_id})
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc

    if info is None:  # pragma: no cover -- the result expired between the two reads
        return JobStatusResponse(job_id=job_id, status="not_found")
    if not info.success:
        return JobStatusResponse(job_id=job_id, status="failed", error=str(info.result))

    return JobStatusResponse(job_id=job_id, status="complete", result=QueryResponse(**info.result))
