# 0004. Async execution: arq on Redis alongside the synchronous endpoint

## Status

Accepted — implemented in `backend/app/jobs/**` (worker + tasks),
`backend/app/api/v1/jobs.py` (enqueue + poll), and the `worker` service in
`docker-compose.yml`.

## Context

A question takes as long as the LLM takes — typically 2–10 s, and much
longer on CPU-only local inference. For a single user with a spinner that
is fine; for a batch, a slow model, or a client behind a proxy with a short
read timeout, it is not. The pipeline itself is already async and stateless,
so the work can move off the request thread — the question is what runs it,
and how a caller gets the answer back.

Redis is already in the stack for the cache (ADR 0003), which makes a
Redis-backed queue nearly free operationally.

## Decision

**Keep the synchronous endpoint and add an async one beside it**, both
running the identical pipeline:

1. `POST /api/v1/query` stays the common path and the default in the UI.
2. `POST /api/v1/query/async` takes the *same* `QueryRequest` body and
   returns `202 {job_id, status}`; `GET /api/v1/jobs/{job_id}` polls and
   returns the *same* `QueryResponse` under `result` once complete. One
   request schema and one response schema for both paths
   (`app/services/query_service.py`), so a client can switch between them
   without remapping anything.
3. **arq, not Celery.** It is asyncio-native (the task function is a
   coroutine running on the same stack as the pipeline), Redis-only, and
   configured in a few dozen lines. Celery's rate limits, chords, and
   multiple brokers are not needed here and would cost a second dependency
   plus a synchronous execution model.
4. **One runtime builder for both processes.** The worker's `on_startup`
   calls the same `app.services.bootstrap.build_components()` the FastAPI
   lifespan does, so a job goes through the same pipeline, the same caches,
   and the same schema version as the synchronous route — there is no
   second, drifting copy of the wiring. The worker deliberately does *not*
   migrate the app database: the API process owns that, so the two never
   race Alembic — and compose starts the worker only once `backend` is
   healthy, so the migration is finished before the first job can write.
5. **Polling, not SSE/WebSocket.** Polling works through any ingress and
   any proxy, needs no connection state, and is three lines in the
   frontend. Streaming is a stretch goal, not a Phase 2 requirement.
6. **A pipeline error is not a job failure.** A guard rejection, a failed
   statement, or an LLM outage completes the job normally with the message
   in `result.error`, exactly as `POST /query` reports it. `status="failed"`
   is reserved for a genuine bug — an unexpected exception in the task
   function — and carries the exception text in `error`.
7. **Queue depth is a metric.** The API process samples `ZCARD` of the arq
   queue every `QUEUE_DEPTH_SAMPLE_S` (default 5 s) into
   `t2s_jobs_queue_depth` (ADR 0005).
8. **Degrade, don't fail.** If Redis is unreachable at startup the arq pool
   is `None` and both async routes answer 503; `POST /query` keeps working.

## Alternatives considered

- **FastAPI `BackgroundTasks`.** Zero new infrastructure, but the work runs
  in the API process: it dies with a restart, competes with request
  handling for the event loop, and cannot be scaled or observed
  separately. Fine for fire-and-forget side effects, not for the primary
  workload.
- **Celery.** The default answer, and the right one if you need its
  feature set or a non-Redis broker. Here it would add a second
  dependency and a synchronous worker model at odds with the async
  pipeline.
- **SSE or WebSocket job updates.** Better UX (no poll interval, no wasted
  requests), at the cost of connection state through the ingress and more
  frontend machinery. Deferred.
- **Async-only.** Removing the synchronous path would simplify the API but
  make the common case — one question, one answer, in a browser — strictly
  worse.

## Consequences

- Polling costs one request per interval per in-flight job. At demo scale
  this is noise; at real scale it is the first thing to replace.
- Job results live in Redis for arq's default retention (1 hour) and job
  ids are unauthenticated — anyone who knows an id can read that job's
  answer. Acceptable for a local, single-user stack; not for a
  multi-tenant deployment.
- arq pickles job results, so `run_query_job` returns a plain dict
  (`QueryResponse.model_dump()`) rather than a pydantic model — a pickled
  model would tie every future reader to today's class definition.
- **A refresh propagates to every process, through a Redis epoch.**
  Whoever re-introspects — `POST /api/v1/schema/refresh` in an API process,
  `refresh_schema_job` in the worker — calls
  `app.services.schema_sync.mark_refreshed`, which writes a fresh opaque
  token to `schema:epoch`. Every process runs a watcher loop
  (`watch_schema_epoch`, started by `app.main`'s lifespan and by the
  worker's `on_startup`) that reads that key every `SCHEMA_SYNC_POLL_S`
  seconds (default `5`; `0` disables it) and, when the token differs from
  the one it has already accounted for, runs the same `refresh_components`
  on itself and adopts it. A process built *after* someone else's refresh
  takes the current token as its baseline, so it never re-refreshes for
  history. With Redis down or `CACHE_ENABLED=false` the mechanism no-ops:
  the refresh still applies locally and the other processes simply never
  hear about it — the pre-epoch behaviour, never an error.

  Polling rather than pub/sub, deliberately. `RedisCache` is
  request/response only; a subscriber would need a second, long-lived
  connection type, with its own failure and reconnection modes, for a
  signal sent a handful of times a day. Polling also survives the case
  pub/sub handles worst: a process that was restarting when the message
  went out has missed it forever, while a poller catches up on its next
  tick. The cost is bounded staleness of `SCHEMA_SYNC_POLL_S` and one `GET`
  per process per tick.

  This also settles PLAN.md item 3's "schema refresh also becomes a job":
  `refresh_schema_job` *is* that job, and with propagation in place the
  worker's refresh reaches the API and the API's reaches the worker — so
  no HTTP trigger for the job is needed, `POST /api/v1/schema/refresh`
  already refreshes the whole deployment.
- **The worker's metrics are scraped.** It builds a `MetricsObserver` for
  free with `build_components`, and `on_startup` serves that registry on
  `WORKER_METRICS_PORT` (default `9100`) from a WSGI daemon thread
  (`app/observability/exporter.py`), scraped as the `text2sql-worker` job.
  Its series carry Prometheus's `job="text2sql-worker"` label, so the
  dashboard's `sum(...)` panels aggregate both processes and a per-process
  view is a `by (job)` away — see ADR 0005. `t2s_jobs_queue_depth` stays
  API-side: it samples the queue, not the work, so one sampler is right.
  The structured logs remain the per-job record.
- Two processes now build the same runtime at startup, which doubles
  connections to Postgres, Redis, and the LLM at boot. That is the price of
  running the identical code in both, and it is the right trade at this
  size.
