"""Integration tests for the arq background worker (`app.jobs.**`) and the
async query endpoints (`POST /api/v1/query/async`, `GET /api/v1/jobs/{id}`).

Everything here runs against the real services: the seeded container
database, the real `redis_url` container, the real `create_app()` lifespan
(which opens its own `ArqRedis` enqueue pool), and a real
`arq.worker.Worker` driven in-process in `burst=True` mode -- no mocks. The
worker's `ctx` is seeded with components built from `build_components(...,
llm=FakeLLM(...))`, which is exactly how `WorkerSettings.on_startup` builds
them in production, only with a `FakeLLM` in place of the real client --
mirrors `test_api.py`/`test_query_cache.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis import RedisCache
from app.config import Settings
from app.db.models import QueryLog
from app.db.session import make_engine
from app.jobs.tasks import refresh_schema_job, run_query_job
from app.llm.base import LLMClient
from app.main import create_app
from app.services.bootstrap import build_components, close_components
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration

QUESTION = "How many orders are there?"
GOOD_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM orders", "explanation": "Counts all orders."}
)
DELETE_QUESTION = "Delete every order please"
DELETE_RESPONSE = json.dumps({"sql": "DELETE FROM orders", "explanation": "Deletes all orders."})


@pytest.fixture
def settings(app_db_url: str, readonly_async_url: str, redis_url: str) -> Settings:
    return Settings(
        app_db_url=app_db_url,
        target_db_url=readonly_async_url,
        redis_url=redis_url,
        # Short enough that the queue-depth test below can observe a sample
        # without a multi-second wait.
        queue_depth_sample_s=0.2,
        _env_file=None,
    )


@pytest.fixture(autouse=True)
async def _clean_redis(redis_url: str) -> AsyncIterator[None]:
    """Drop every `arq:` and `t2s:` key before *and* after each test.

    The `redis_url` container is session-scoped, so without this an earlier
    test would leave a queued job or a cached SQL/schema entry behind that
    this module's assertions (about job state, and about `FakeLLM` actually
    being called) would trip over -- and, symmetrically, this module would
    leave a warm schema cache behind for `test_metrics_endpoint.py`, which
    asserts on a *cold* one.
    """
    arq_keys = RedisCache(redis_url, namespace="arq")
    t2s_keys = RedisCache(redis_url)

    async def clean() -> None:
        await arq_keys.delete_prefix("")
        await t2s_keys.delete_prefix("")

    await clean()
    yield
    await clean()
    await arq_keys.aclose()
    await t2s_keys.aclose()


@asynccontextmanager
async def _api(settings: Settings, llm: LLMClient) -> AsyncIterator[AsyncClient]:
    """The real app, lifespan and all, wrapped in an `AsyncClient`."""
    app = create_app(settings=settings, llm=llm)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        # `follow_redirects=True`: `app.mount("/metrics", ...)` 307s a
        # trailing-slash-less `GET /metrics` (Starlette `Mount` default).
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=True
        ) as client:
            yield client


async def _drain_queue(settings: Settings, llm: LLMClient) -> None:
    """Run a real arq worker in-process until the queue is empty
    (`burst=True`), against the same Redis the API enqueued into.

    The worker's `ctx` is seeded directly, which is how arq lets a caller
    supply a job context without an `on_startup` hook -- so the production
    `WorkerSettings.on_startup` needs no test-only injection seam.

    Teardown closes the pool by hand rather than via `Worker.close()`:
    `close()` raises `signal.SIGUSR1` at a worker started with
    `handle_signals=False`, and that signal does not exist on Windows. In
    `burst=True` mode `async_run()` has already gathered every job task
    before it returns, so there is nothing else left to wind down.
    """
    components = await build_components(settings, llm=llm)
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    worker = Worker(
        functions=[run_query_job, refresh_schema_job],
        redis_pool=pool,
        burst=True,
        poll_delay=0.1,
        max_tries=1,
        # Installing signal handlers inside the pytest event loop would
        # clobber the test runner's own.
        handle_signals=False,
        ctx={"components": components},
    )
    try:
        await worker.async_run()
    finally:
        await pool.aclose()
        await close_components(components)


async def _query_log_rows(app_db_url: str, request_id: str) -> list[QueryLog]:
    engine = make_engine(app_db_url)
    try:
        async with AsyncSession(engine) as session:
            result = await session.execute(
                select(QueryLog).where(QueryLog.request_id == request_id)
            )
            return list(result.scalars().all())
    finally:
        await engine.dispose()


def _gauge_value(metrics_text: str, name: str) -> float | None:
    for line in metrics_text.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split(" ", 1)[1])
    return None


async def test_enqueued_query_runs_on_the_worker_and_reports_complete(
    settings: Settings, app_db_url: str
) -> None:
    async with _api(settings, FakeLLM([])) as client:
        enqueued = await client.post("/api/v1/query/async", json={"question": QUESTION})
        assert enqueued.status_code == 202
        job_id = enqueued.json()["job_id"]
        assert enqueued.json()["status"] == "queued"
        request_id = enqueued.headers["X-Request-ID"]

        # Nothing consumes the queue yet, so the job is still waiting.
        pending = await client.get(f"/api/v1/jobs/{job_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "queued"
        assert pending.json()["result"] is None

        await _drain_queue(settings, FakeLLM([GOOD_RESPONSE]))

        done = await client.get(f"/api/v1/jobs/{job_id}")

    assert done.status_code == 200
    body = done.json()
    assert body["job_id"] == job_id
    assert body["status"] == "complete"
    assert body["error"] is None

    result = body["result"]
    assert result["error"] is None
    # The guard normalises the generated SQL (it appends the `max_rows`
    # LIMIT), so match on shape rather than the exact string.
    assert result["sql"].upper().startswith("SELECT")
    assert "orders" in result["sql"].lower()
    assert result["columns"] == ["n"]
    assert result["row_count"] == 1
    assert result["rows"][0][0] > 0
    # The enqueueing request's id rides along into the job's result and its
    # `query_log` row, so an async query is traceable end to end.
    assert result["request_id"] == request_id

    rows = await _query_log_rows(app_db_url, request_id)
    assert len(rows) == 1
    assert rows[0].success is True
    assert rows[0].question == QUESTION


async def test_unknown_job_id_reports_not_found(settings: Settings) -> None:
    async with _api(settings, FakeLLM([])) as client:
        resp = await client.get("/api/v1/jobs/no-such-job")

    assert resp.status_code == 200
    assert resp.json() == {
        "job_id": "no-such-job",
        "status": "not_found",
        "result": None,
        "error": None,
    }


async def test_pipeline_error_completes_the_job_with_the_error_in_the_result(
    settings: Settings,
) -> None:
    """A guard rejection is a *pipeline* error, not a job failure: the task
    returns normally, so the job is `complete` and the error surfaces inside
    `result.error` -- exactly as `POST /api/v1/query` reports it."""
    async with _api(settings, FakeLLM([])) as client:
        enqueued = await client.post(
            "/api/v1/query/async", json={"question": DELETE_QUESTION, "use_cache": False}
        )
        job_id = enqueued.json()["job_id"]

        await _drain_queue(settings, FakeLLM([DELETE_RESPONSE]))

        done = await client.get(f"/api/v1/jobs/{job_id}")

    body = done.json()
    assert body["status"] == "complete"
    assert body["error"] is None
    assert body["result"]["error"].startswith("guard:not_select")
    assert body["result"]["rows"] == []
    # The request body's `use_cache` rode through the queue into the
    # pipeline: `QueryRequest` means the same thing on both endpoints.
    assert body["result"]["cache_status"] == "bypass"


async def test_use_result_cache_travels_through_the_queue(settings: Settings) -> None:
    """The async endpoint takes the very same `QueryRequest` as the sync one,
    so `use_result_cache` has to reach the pipeline too: the second job
    answers from the result cache without an LLM call at all (its `FakeLLM`
    has no canned responses left to give)."""
    body = {"question": QUESTION, "use_result_cache": True}

    async with _api(settings, FakeLLM([])) as client:
        first_id = (await client.post("/api/v1/query/async", json=body)).json()["job_id"]
        await _drain_queue(settings, FakeLLM([GOOD_RESPONSE]))
        first = await client.get(f"/api/v1/jobs/{first_id}")

        second_id = (await client.post("/api/v1/query/async", json=body)).json()["job_id"]
        await _drain_queue(settings, FakeLLM([]))
        second = await client.get(f"/api/v1/jobs/{second_id}")

    assert first.json()["result"]["cache_status"] == "miss"
    assert second.json()["status"] == "complete"
    assert second.json()["result"]["cache_status"] == "result_hit"
    assert second.json()["result"]["rows"] == first.json()["result"]["rows"]


async def test_refresh_schema_job_runs_on_the_worker(settings: Settings) -> None:
    """The worker's other registered function, driven the same way: enqueued
    onto the real queue and run by the real worker."""
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        job = await pool.enqueue_job("refresh_schema_job")
        assert job is not None

        await _drain_queue(settings, FakeLLM([]))

        info = await job.result_info()
    finally:
        await pool.aclose()

    assert info is not None
    assert info.success is True
    assert set(info.result) == {"version"}
    assert info.result["version"]


async def test_enqueue_returns_503_when_redis_is_unreachable_but_sync_query_still_works(
    settings: Settings,
) -> None:
    down = settings.model_copy(update={"redis_url": "redis://localhost:1/0"})

    async with _api(down, FakeLLM([GOOD_RESPONSE])) as client:
        enqueued = await client.post("/api/v1/query/async", json={"question": QUESTION})
        sync = await client.post("/api/v1/query", json={"question": QUESTION})

    assert enqueued.status_code == 503
    assert "queue" in enqueued.json()["detail"].lower()
    # The cache/queue being down must never take the synchronous path with
    # it -- see the Phase 2 plan's "cache is transparent" constraint.
    assert sync.status_code == 200
    assert sync.json()["error"] is None


async def test_queue_depth_gauge_tracks_pending_jobs(settings: Settings) -> None:
    async with _api(settings, FakeLLM([])) as client:
        assert (await client.post("/api/v1/query/async", json={"question": QUESTION})).status_code
        depth: float | None = None
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            metrics = await client.get("/metrics")
            depth = _gauge_value(metrics.text, "t2s_jobs_queue_depth")
            if depth:
                break
            await asyncio.sleep(0.1)

    assert depth == 1.0
