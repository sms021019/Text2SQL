# Phase 2 — Caching, Async Queue, Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Same demo as v0.1, now with a Redis two-tier cache (SQL + results) with a cache-hit indicator in the UI, an arq worker running long queries in the background, and a Grafana dashboard that moves under k6 load.

**Architecture:** Redis added as the only new stateful dependency. `app/cache` wraps it (schema cache keyed by schema version; SQL cache keyed by `hash(schema_version, model, prompt_version, normalised question)`; short-TTL result cache opt-in). `app/observability/metrics.py` owns a single Prometheus registry; every stage of the pipeline is instrumented via small hooks so `app/core` stays framework-free (metrics are recorded by a `PipelineObserver` protocol injected from the app layer). arq runs the same `Text2SQLPipeline` in a worker process; the API enqueues and polls. Prometheus + Grafana are compose services with a committed provisioned dashboard.

**Tech Stack:** redis-py (asyncio), arq, prometheus_client, prometheus-fastapi-instrumentator, testcontainers[redis], k6 (docker image), Grafana 11, Prometheus 2.x.

**Spec:** `PLAN.md` — §3 Phase 2 (items 1–8), §4 D3 (cache), D4 (sync/async), D8 (observability).

## Global Constraints

- Everything in Phase 1's Global Constraints still applies (Python 3.12; `app/core/**` imports no `fastapi`, `redis`, `arq`, `prometheus_client`, or `app.api`; AST guard; read-only role; offline tests via `FakeLLM`; LF endings; `python -m uv run` from `backend/`).
- Cached *errors* are never stored. Cache is transparent: with Redis down the API must still answer (log + miss).
- Metric labels are low-cardinality only: `provider`, `model`, `stage`, `reason`, `outcome`, `cache`, `direction`. Never question text, request id, or SQL.
- Metric names exactly as PLAN.md §3 Phase 2 item 4 (`t2s_*`).
- Every phase leaves the repo demoable: `make up` must keep working after every task; `make test` green.
- Commit after every task; trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## File map (new/modified this phase)

| Path | Responsibility |
|---|---|
| `backend/app/cache/__init__.py`, `redis.py` | `RedisCache` wrapper: connection, namespaced keys, safe get/set (swallow+log connection errors), `ping()` |
| `backend/app/cache/keys.py` | `normalise_question()`, `sql_cache_key()`, `result_cache_key()`, `schema_cache_key()` |
| `backend/app/cache/schema_cache.py` | persist/load `SchemaGraph` + embeddings matrix by version |
| `backend/app/cache/query_cache.py` | `QueryCache`: two-tier SQL/result get/set with TTLs |
| `backend/app/core/observer.py` | `PipelineObserver` Protocol + `NullObserver` (core-side hook, no deps) |
| `backend/app/core/pipeline.py` (modify) | accepts `observer` and `query_cache`-like `SqlCache` Protocol; emits stage events; SQL-cache short-circuit |
| `backend/app/observability/metrics.py` | registry + all `t2s_*` metrics + `MetricsObserver(PipelineObserver)` + price table |
| `backend/app/jobs/__init__.py`, `worker.py`, `tasks.py` | arq `WorkerSettings`, `run_query_job`, `refresh_schema_job`, startup builds pipeline |
| `backend/app/api/v1/jobs.py` | `POST /api/v1/query/async`, `GET /api/v1/jobs/{id}` |
| `backend/app/api/v1/query.py` (modify) | `use_cache` flag, `cached` field |
| `backend/app/main.py` (modify) | Redis + metrics + arq pool in lifespan; `/metrics` |
| `backend/tests/conftest.py` (modify) | `redis_url` fixture (testcontainers) |
| `backend/tests/fakes/redis.py` | none — use real Redis container (integration) and `fakeredis` for unit tests |
| `deploy/prometheus/prometheus.yml`, `deploy/grafana/provisioning/**`, `deploy/grafana/dashboards/text2sql.json` | observability stack |
| `scripts/load_test.js` | k6 replay of `questions.yaml` |
| `docker-compose.yml` (modify) | `redis`, `worker`, `prometheus`, `grafana` services |
| `frontend/src/**` (modify) | async toggle + polling, cached badge |
| `docs/decisions/0003-cache-design.md`, `0004-async-jobs.md`, `0005-observability.md` | ADRs |

---

### Task 1: Redis wrapper, settings, fixtures, compose service

**Files:**
- Create: `backend/app/cache/__init__.py`, `backend/app/cache/redis.py`, `backend/app/cache/keys.py`
- Modify: `backend/app/config.py`, `backend/pyproject.toml`, `backend/tests/conftest.py`, `docker-compose.yml`, `.env.example`
- Test: `backend/tests/unit/test_cache_keys.py`, `backend/tests/integration/test_redis_cache.py`

**Interfaces:**
```python
# config.py additions
redis_url: str = "redis://localhost:6379/0"
cache_enabled: bool = True
sql_cache_ttl_s: int = 86400
result_cache_ttl_s: int = 300
schema_cache_ttl_s: int = 604800

# cache/redis.py
class RedisCache:
    def __init__(self, url: str, *, namespace: str = "t2s", enabled: bool = True): ...
    async def get_json(self, key: str) -> Any | None          # None on miss OR on connection error (logged)
    async def set_json(self, key: str, value: Any, *, ttl_s: int) -> bool   # False when skipped/failed
    async def get_bytes/set_bytes(...)                         # for numpy embeddings
    async def delete_prefix(self, prefix: str) -> int          # SCAN + DEL
    async def ping(self) -> bool
    async def aclose(self) -> None
    @property def last_error(self) -> str | None

# cache/keys.py
def normalise_question(q: str) -> str          # lower, collapse whitespace, strip trailing ?.!;
def sql_cache_key(*, schema_version, model, prompt_version, question) -> str   # "sql:{sha256[:32]}"
def result_cache_key(*, sql_key: str) -> str                                   # "res:{same hash}"
def schema_cache_key(version: str) -> str                                      # "schema:{version}"
```

- [ ] **Step 1: Deps.** Add `redis>=5` to deps; `testcontainers[redis]`, `fakeredis>=2.23` to dev. `python -m uv lock`.
- [ ] **Step 2: Unit tests** for `keys.py`: normalisation examples (`"How many ORDERS?  "` → `"how many orders"`), key stability, key changes when any component changes, prefix format.
- [ ] **Step 3: Integration tests** with `redis_url` fixture (`RedisContainer("redis:7-alpine")`): set/get roundtrip with TTL, `delete_prefix`, and **graceful degradation**: `RedisCache("redis://localhost:1")` → `get_json` returns None, `set_json` returns False, `last_error` set, no exception.
- [ ] **Step 4: Implement**; `enabled=False` short-circuits everything. Use `redis.asyncio.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)`.
- [ ] **Step 5: compose** `redis` service (`redis:7-alpine`, healthcheck `redis-cli ping`, `127.0.0.1:6379`), backend env `REDIS_URL=redis://redis:6379/0`, `depends_on`. `.env.example` gets `REDIS_URL`, `CACHE_ENABLED`, TTLs.
- [ ] **Step 6:** lint, full suite, commit `feat(cache): redis wrapper, keys, fixtures`.

---

### Task 2: Schema cache (graph + embeddings) and refresh invalidation

**Files:**
- Create: `backend/app/cache/schema_cache.py`
- Modify: `backend/app/core/schema/retrieve.py` (export/import index), `backend/app/main.py`, `backend/app/api/v1/schema.py`
- Test: `backend/tests/integration/test_schema_cache.py`

**Interfaces:**
```python
# retrieve.py additions
def export_index(self) -> tuple[list[str], NDArray[np.float64]] | None
def import_index(self, names: list[str], matrix: NDArray[np.float64]) -> None   # marks built
# schema_cache.py
class SchemaCache:
    def __init__(self, cache: RedisCache, *, ttl_s: int): ...
    async def load(self, version: str) -> tuple[SchemaGraph, list[str], NDArray] | None
    async def store(self, graph: SchemaGraph, names, matrix) -> None
    async def invalidate_all(self) -> int     # delete_prefix("schema:")
```
- Lifespan: introspect (cheap, always) → `version` → `schema_cache.load(version)`; on hit `import_index` and skip the embed call (startup no longer needs the LLM when warm); on miss embed (existing bounded path) then `store`. `/schema/refresh` → `invalidate_all()` then same flow. Graph JSON via pydantic `model_dump_json`; matrix via `np.save` to bytes.
- [ ] Tests: cold start stores; second app start with the same Redis hits and makes **zero** embed calls (FakeLLM call count 0); refresh invalidates and re-stores; version mismatch is a miss.
- [ ] Commit `feat(cache): persist schema graph and embeddings in redis`.

---

### Task 3: Two-tier query cache in the pipeline

**Files:**
- Create: `backend/app/cache/query_cache.py`, `backend/app/core/observer.py`
- Modify: `backend/app/core/pipeline.py`, `backend/app/api/v1/query.py`, `backend/app/main.py`
- Test: `backend/tests/unit/test_pipeline_cache.py` (fakeredis + FakeLLM + stub executor? — no: integration), `backend/tests/integration/test_query_cache.py`

**Interfaces:**
```python
# core/observer.py  (no external imports)
class PipelineObserver(Protocol):
    def on_stage(self, stage: str, ms: float) -> None: ...
    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None: ...
    def on_guard_reject(self, reason: str) -> None: ...
    def on_execution(self, *, outcome: str, ms: float) -> None: ...     # success|db_error|timeout|rejected
    def on_cache(self, *, cache: str, outcome: str) -> None: ...         # cache: sql|result|schema ; outcome: hit|miss|bypass
    def on_pipeline_done(self, *, ms: float, ok: bool) -> None: ...
class NullObserver: ...
# core/pipeline.py  — SqlCache Protocol (core-side, duck-typed)
class SqlCache(Protocol):
    async def get_sql(self, key: str) -> tuple[str, str] | None          # (sql, explanation)
    async def set_sql(self, key: str, sql: str, explanation: str) -> None
    async def get_result(self, key: str) -> QueryResult | None
    async def set_result(self, key: str, result: QueryResult) -> None
Text2SQLPipeline(..., observer: PipelineObserver = NullObserver(), cache: SqlCache | None = None)
async def run(self, question: str, *, use_cache: bool = True, use_result_cache: bool = False) -> PipelineOutput
PipelineOutput gains: cache_status: Literal["miss","sql_hit","result_hit","bypass","disabled"]
```
- Flow: key = `sql_cache_key(schema_version=graph.version, model=settings.llm_model, prompt_version, question)`. If `use_result_cache` and result hit → return immediately (no LLM, no DB) with `cache_status="result_hit"`. Else if SQL hit → skip retrieve/generate/parse, still run guard (cheap, defence) + execute, `sql_hit`. Else full path; on success `set_sql` (only after execute succeeded) and if `use_result_cache` `set_result`. Repaired SQL is what gets cached. Errors never cached.
- API: `QueryRequest.use_cache: bool = True`, `use_result_cache: bool = False`; response adds `cache_status`.
- [ ] Tests (integration, real Redis + FakeLLM): second identical question → `sql_hit`, 1 LLM call total; result-cache hit → 0 DB calls (assert via timings lacking `execute`); different question/model → miss; guard-rejected SQL never stored; `use_cache=False` → `bypass`; Redis unreachable → `miss` and still works.
- [ ] Commit `feat(cache): two-tier sql/result cache with observer hooks`.

---

### Task 4: Prometheus metrics

**Files:**
- Create: `backend/app/observability/metrics.py`
- Modify: `backend/app/main.py`, `backend/app/llm/*.py`? (no — usage flows through observer), `backend/app/cache/*.py` (call `observer.on_cache` from pipeline, schema cache from lifespan), `backend/app/config.py` (`llm_prices_usd_per_1k: dict[str, tuple[float,float]] = {"gpt-4o-mini": (0.00015, 0.0006), ...}`)
- Test: `backend/tests/unit/test_metrics.py`, `backend/tests/integration/test_metrics_endpoint.py`

**Interfaces:** metric names/labels exactly:
```
t2s_llm_request_duration_seconds{provider,model,stage}  histogram buckets (0.25,0.5,1,2,4,8,16,30,60)
t2s_llm_tokens_total{provider,model,direction}
t2s_llm_cost_usd_total{provider,model}
t2s_sql_guard_rejections_total{reason}
t2s_sql_execution_total{outcome}
t2s_sql_execution_duration_seconds        histogram
t2s_cache_requests_total{cache,outcome}
t2s_pipeline_duration_seconds{ok}         histogram
t2s_jobs_queue_depth                      gauge (set by Task 5)
t2s_schema_version_info{version}          info gauge
```
`MetricsObserver(provider: str)` implements `PipelineObserver`. `prometheus-fastapi-instrumentator` adds HTTP metrics and exposes `/metrics` (exclude `/metrics`, `/healthz`, `/readyz` from request histograms). Use a **custom registry** stored on `app.state.metrics` so tests can construct fresh ones; expose via `make_asgi_app(registry)` mounted at `/metrics`.
- [ ] Unit tests: observer calls produce expected samples (`registry.get_sample_value`); cost computed from price table, 0 for unknown model. Integration: `/metrics` returns text containing `t2s_pipeline_duration_seconds` after a query.
- [ ] Commit `feat(observability): prometheus metrics and /metrics endpoint`.

---

### Task 5: arq worker + async endpoints

**Files:**
- Create: `backend/app/jobs/__init__.py`, `worker.py`, `tasks.py`, `backend/app/api/v1/jobs.py`, `backend/app/services/bootstrap.py` (extract "build pipeline from settings" out of `main.py` so worker and API share it)
- Modify: `backend/app/main.py`, `backend/app/api/router.py`, `docker-compose.yml`, `backend/Dockerfile` (no change; compose overrides `command: arq app.jobs.worker.WorkerSettings`)
- Test: `backend/tests/integration/test_jobs.py`

**Interfaces:**
```python
# services/bootstrap.py
@dataclass class AppComponents: settings, target_engine, app_engine, session_factory, llm, redis, schema_cache, query_cache, graph, retriever, builder, pipeline, metrics(observer)
async def build_components(settings, *, llm=None, observer=None) -> AppComponents
async def close_components(c: AppComponents) -> None
# jobs/tasks.py
async def run_query_job(ctx, question: str, *, use_cache: bool, request_id: str) -> dict   # PipelineOutput → QueryResponse-shaped dict, also record_query
async def refresh_schema_job(ctx) -> dict[str, str]
# jobs/worker.py
class WorkerSettings: functions=[run_query_job, refresh_schema_job]; on_startup builds components into ctx; on_shutdown closes; redis_settings from REDIS_URL; max_jobs=4; job_timeout=300
# api/v1/jobs.py
POST /api/v1/query/async {question, use_cache} -> 202 {"job_id": str, "status": "queued"}
GET  /api/v1/jobs/{job_id} -> {"job_id", "status": queued|in_progress|complete|failed|not_found, "result": QueryResponse|None, "error": str|None}
```
- Queue depth gauge: a background task in the API lifespan samples `await redis.zcard("arq:queue")` every 5 s into `t2s_jobs_queue_depth`.
- compose: `worker` service (same image, `command: ["arq", "app.jobs.worker.WorkerSettings"]`, depends on postgres+redis healthy, no ports).
- [ ] Tests: run an arq `Worker` in-process (`arq.worker.Worker(functions=..., redis_settings=..., burst=True)`) against the container Redis with FakeLLM; enqueue via API → poll → `complete` with rows; unknown id → `not_found`; failed pipeline surfaces `error` in result (status complete, result.error set).
- [ ] Commit `feat(jobs): arq worker and async query endpoints`.

---

### Task 6: Frontend — async mode, polling, cache badge

**Files:** modify `frontend/src/api.ts`, `App.tsx`, `components/QuestionForm.tsx`, `SqlPanel.tsx`, new `components/JobStatus.tsx`, tests.
- `api.ts`: `askQuestionAsync()`, `getJob(id)`, types for job status; `QueryResponse.cache_status`.
- UI: "Run in background" checkbox; when on, POST async then poll every 1 s (max 5 min) showing a `JobStatus` pill (queued/in_progress); result renders identically. `SqlPanel` badge `cache: sql hit | result hit | miss`. "Use cache" checkbox (default on).
- [ ] vitest: polling flow with fake timers; badge rendering. `npm run build`, `npm test`. Commit `feat(frontend): async jobs and cache indicator`.

---

### Task 7: Prometheus + Grafana + k6 + docs, tag v0.2

**Files:** `deploy/prometheus/prometheus.yml`, `deploy/grafana/provisioning/datasources/prometheus.yml`, `deploy/grafana/provisioning/dashboards/dashboards.yml`, `deploy/grafana/dashboards/text2sql.json`, `scripts/load_test.js`, `docker-compose.yml` (`prometheus` on 127.0.0.1:9090, `grafana` on 127.0.0.1:3000 with anonymous viewer + admin/admin, both under profile `observability`? — **no**: always on, they're the demo), `Makefile` (`load-test: docker run --rm -i --network host grafana/k6 run - <scripts/load_test.js`), `docs/demo.md`, `README.md`, ADRs 0003–0005.
- Dashboard panels: p50/p95 LLM latency by stage; SQL success rate (`sum(rate(t2s_sql_execution_total{outcome="success"}[5m])) / sum(rate(t2s_sql_execution_total[5m]))`); cache hit rate by cache; tokens/min and cost; queue depth; HTTP p95; guard rejections by reason.
- k6: reads questions via `open('../seed/questions.yaml')`? k6 can't parse YAML → generate `scripts/questions.json` from yaml at `make load-test` time via a tiny python step, or embed 10 questions inline. Choose: `scripts/load_test.js` with `__ENV.API_URL`, 5 VUs × 60 s, alternating `use_cache` on/off so the hit-rate panel moves.
- Verification: `make up` → query twice (second `sql_hit`) → `curl :8000/metrics | grep t2s_cache_requests_total` → Grafana dashboard loads at :3000 with data → `make load-test` (with FakeLLM impossible; with no LLM the run still exercises guard/cache/miss paths — document).
- [ ] Commit `feat(observability): prometheus + grafana stack, k6 load test, docs`; controller tags `v0.2` after final review.

---

## Self-review
- Spec coverage: PLAN.md Phase 2 items 1 (T1–T3), 2 (T2), 3 (T5, T6), 4 (T4, T5 gauge), 5 (T7), 6 (already in v0.1; exemplars out of scope), 7 (T7), 8 (T1, T3, T5 tests). ✔
- Core boundary: `app/core` gains only `observer.py` and a duck-typed `SqlCache` Protocol — no redis/prometheus imports. ✔
- Names consistent: `RedisCache`, `SchemaCache`, `QueryCache`, `PipelineObserver`, `MetricsObserver`, `build_components`, `run_query_job`. ✔
