# Phase 2 Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six gaps the Phase 2 ADRs and final review documented as "deliberate follow-ups": `query_log.cache_status`, an observed `embed` LLM stage, a scraped worker `/metrics`, cross-process schema-refresh propagation, `GET /jobs/{id}` on a non-query job, and worker container hardening.

**Architecture:** Every change extends an existing seam rather than adding a subsystem. `LLMClient.embed` grows a `Usage`-bearing return type so `SchemaRetriever` can report `stage="embed"` through the existing `PipelineObserver`. The worker serves its existing `Metrics.registry` over `prometheus_client.start_http_server` and Prometheus gets a second scrape job. Schema refreshes propagate through a Redis "epoch" key: whoever refreshes bumps it, every process polls it (API lifespan task, worker startup task) and calls the existing `refresh_components` when it changes. `record_query` writes the `cache_status` it already receives on `PipelineOutput`.

**Tech Stack:** FastAPI, SQLAlchemy 2 + Alembic, redis-py 5 (`RedisCache` wrapper), arq 0.28, prometheus_client, httpx, pytest + testcontainers (Postgres, Redis), FakeLLM.

**Spec:** `PLAN.md` §3 Phase 2 items 3 (schema refresh as a job), 4 (`stage=embed`, `t2s_jobs_queue_depth`), 5/8; the follow-up bullets in `docs/decisions/0003-cache-design.md:119-124`, `0004-async-jobs.md:88-98`, `0005-observability.md:98-134`; README "Known gaps" (`README.md:298-305`).

## Global Constraints

- Everything in the Phase 2 plan's Global Constraints still applies: Python 3.12; `app/core/**` imports no `fastapi`, `redis`, `arq`, `prometheus_client`, or `app.api`; cached errors never stored; cache transparent (Redis down ⇒ API still answers); metric labels low-cardinality only (`provider`, `model`, `stage`, `reason`, `outcome`, `cache`, `direction`, `ok`, `version`); LF endings; `python -m uv run` from `backend/`; `make up` keeps working; `make test` and `make lint` green after every task.
- No new dependencies. `prometheus_client`, `redis`, `arq`, `alembic` are already present.
- `PipelineOutput.usage` keeps its meaning (generate + repair tokens/latency). Embed usage goes to metrics only — never into `PipelineOutput.usage` or `query_log.prompt_tokens`.
- New settings get a `.env.example` line and a `#:` docstring in `backend/app/config.py`, matching the existing style.
- Docs that describe a gap this plan closes must be updated in the same task that closes it (ADR bullets, README "Known gaps", `docs/architecture.md`, dashboard panel descriptions). Do not leave a doc claiming a gap that no longer exists.
- Commit after every task; trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Work on branch `phase2-followups` (already created from `main` @ `bbac5d5`).

---

## File map (new/modified this plan)

| Path | Responsibility |
|---|---|
| `backend/alembic/versions/0002_query_log_cache_status.py` | add nullable `cache_status` column |
| `backend/app/db/models.py`, `backend/app/db/query_log.py` | `QueryLog.cache_status`; `record_query` writes it |
| `backend/app/llm/base.py` | `EmbeddingResult` dataclass; `LLMClient.embed -> EmbeddingResult` |
| `backend/app/llm/ollama.py`, `openai_compat.py`, `backend/tests/fakes/llm.py` | timed, usage-bearing `embed` |
| `backend/app/core/schema/retrieve.py` | `SchemaRetriever(observer=...)`; `on_llm(stage="embed")` at both embed sites |
| `backend/app/services/schema_service.py` | `prepare_retriever(..., llm_observer=...)` passes observer into `SchemaRetriever` |
| `backend/app/observability/exporter.py` | `start_metrics_server(registry, port) -> MetricsServer` (thread-backed exposition for the worker) |
| `backend/app/jobs/worker.py` | start/stop the metrics server and the schema-epoch watcher |
| `backend/app/cache/schema_cache.py` | `get_epoch()` / `bump_epoch()` on the `schema:epoch` key |
| `backend/app/services/schema_sync.py` | `sync_schema_if_stale(components)`, `watch_schema_epoch(...)` loop, `mark_refreshed(components)` |
| `backend/app/services/bootstrap.py` | `AppComponents.schema_epoch`; baseline epoch read at build; `refresh_components` unchanged |
| `backend/app/api/v1/schema.py`, `backend/app/jobs/tasks.py` | call `mark_refreshed` after a refresh |
| `backend/app/main.py` | schema-epoch watcher task in the lifespan (next to the queue-depth sampler) |
| `backend/app/api/v1/jobs.py` | non-`run_query_job` results reported without a `QueryResponse` |
| `backend/app/config.py`, `.env.example` | `worker_metrics_port`, `schema_sync_poll_s` |
| `docker-compose.yml`, `deploy/prometheus/prometheus.yml`, `deploy/grafana/dashboards/text2sql.json` | worker scrape job, worker healthcheck/restart/depends_on, panel description |
| `docs/decisions/0003-*.md`, `0004-*.md`, `0005-*.md`, `README.md`, `docs/architecture.md` | remove closed gaps, describe the new behaviour |
| Tests: `backend/tests/integration/test_query_log.py`, `test_jobs.py`, `test_schema_sync.py` (new), `test_metrics_endpoint.py`; `backend/tests/unit/test_llm_clients.py`, `test_exporter.py` (new), `test_retrieve*.py` (existing retriever tests) | |

---

### Task 1: `query_log.cache_status` column

**Files:**
- Create: `backend/alembic/versions/0002_query_log_cache_status.py`
- Modify: `backend/app/db/models.py:28-48` (`QueryLog`), `backend/app/db/query_log.py:25-51` (`record_query`)
- Modify docs: `docs/decisions/0003-cache-design.md:119-124`, `README.md:298-305`
- Test: `backend/tests/integration/test_query_log.py`

**Interfaces:**
- Consumes: `PipelineOutput.cache_status: CacheStatus` (`backend/app/core/pipeline.py:112,164`).
- Produces: `QueryLog.cache_status: Mapped[str | None]` — `None` only for rows written before this migration; every new row carries one of `miss | sql_hit | result_hit | bypass | disabled`.

- [ ] **Step 1: Write the failing test.** In `backend/tests/integration/test_query_log.py`, extend the success-case assertions (after `assert fetched.request_id == request_id`) and add a dedicated test:

```python
        assert fetched.cache_status == "disabled"  # _fake_output builds cache_status="disabled"
```

```python
async def test_record_query_persists_cache_status(app_db_url: str) -> None:
    upgrade_to_head(app_db_url)
    engine = create_async_engine(app_db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    request_id = str(uuid.uuid4())
    out = replace(_fake_output(), cache_status="sql_hit")
    try:
        async with factory() as session:
            await record_query(
                session, out, question="q", model="m", schema_version="v1", request_id=request_id
            )
        async with factory() as session:
            fetched = (
                await session.execute(select(QueryLog).where(QueryLog.request_id == request_id))
            ).scalar_one()
        assert fetched.cache_status == "sql_hit"
    finally:
        await engine.dispose()
```

(Use the module's existing imports/helpers — `_fake_output`, `upgrade_to_head`, `create_async_engine`, `async_sessionmaker`, `select`; add `from dataclasses import replace` if missing. `PipelineOutput` is a frozen dataclass, so `replace` is the way to vary one field.)

- [ ] **Step 2: Run to verify it fails.** `python -m uv run pytest tests/integration/test_query_log.py -q` → FAIL: `QueryLog` has no attribute `cache_status` / column does not exist.

- [ ] **Step 3: Migration.** Create `backend/alembic/versions/0002_query_log_cache_status.py`:

```python
"""query_log.cache_status

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable on purpose: rows written before this migration have no
    # recorded outcome, and inventing one ("miss") would be a lie. Every
    # row app.db.query_log.record_query writes from now on sets it.
    op.add_column("query_log", sa.Column("cache_status", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("query_log", "cache_status")
```

- [ ] **Step 4: Model + writer.** In `models.py` add after `request_id`:

```python
    #: PipelineOutput.cache_status for this run (miss | sql_hit | result_hit |
    #: bypass | disabled). NULL only on rows older than migration 0002.
    cache_status: Mapped[str | None]
```

In `query_log.py` add `cache_status=out.cache_status,` to the `QueryLog(...)` constructor call and extend the docstring: "`cache_status` is copied from `out.cache_status` so cache outcomes are queryable historically (ADR 0003)."

- [ ] **Step 5: Run tests.** `python -m uv run pytest tests/integration/test_query_log.py tests/integration/test_api.py tests/integration/test_jobs.py -q` → PASS (the existing `test_upgrade_to_head_is_idempotent` now covers 0001→0002).

- [ ] **Step 6: Docs.** `docs/decisions/0003-cache-design.md:119-124`: replace the bullet with: "`query_log.cache_status` (migration 0002) records the per-request outcome, so 'what fraction of last week's questions were cache hits' is a `GROUP BY cache_status` away. Rows from before the migration are NULL." `README.md:298-305`: delete the `query_log` bullet; if the schema-refresh bullet is the only one left, keep the "Known gaps" heading (Task 4 removes the last one).

- [ ] **Step 7: Lint, full suite, commit.** `make lint`, `make test`. Commit `feat(db): record cache_status on query_log (migration 0002)`.

---

### Task 2: Observed `embed` stage

**Files:**
- Modify: `backend/app/llm/base.py`, `backend/app/llm/ollama.py:126-141`, `backend/app/llm/openai_compat.py:216-233`, `backend/tests/fakes/llm.py:285-286`
- Modify: `backend/app/core/schema/retrieve.py` (constructor, `build_index` line 69, `_score_tables` line 157)
- Modify: `backend/app/services/schema_service.py:58-118` (`prepare_retriever`), `backend/app/services/bootstrap.py` (both `prepare_retriever(...)` call sites: `build_components` and `refresh_components:782-788`)
- Modify docs: `docs/decisions/0005-observability.md:120-131`, `deploy/grafana/dashboards/text2sql.json` LLM-latency panel description (the sentence saying no embed stage is emitted), `README.md` Metrics section if it lists stages
- Test: `backend/tests/unit/test_llm_clients.py` (existing — update), the existing retriever unit tests, `backend/tests/unit/test_metrics.py` or `test_observer.py` (add one assertion), `backend/tests/integration/test_metrics_endpoint.py`

**Interfaces:**
- Produces (`backend/app/llm/base.py`):

```python
@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    usage: Usage          # prompt_tokens = tokens embedded (0 if the API does not report), completion_tokens = 0, latency_ms measured
    model: str            # the embed model name


class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion: ...
    async def embed(self, texts: list[str]) -> EmbeddingResult: ...
```

- Produces: `SchemaRetriever.__init__(..., observer: PipelineObserver | None = None)`; both embed call sites emit `self._observer.on_llm(stage="embed", model=result.model, usage=result.usage)`.
- Produces: `prepare_retriever(graph, llm, settings, schema_cache, observer=None, *, llm_observer: PipelineObserver | None = None)` — `observer` keeps its existing meaning (the `cache="schema"` event); `llm_observer` is handed to `SchemaRetriever(observer=...)`.
- Ruling: `bootstrap` passes the *pipeline* observer (the object stored as `AppComponents.observer`) as `llm_observer` at both call sites, so a request-time question embed lands on the same observer chain as `generate`/`repair` (metrics + any injected recording observer). The index-build embed at startup/refresh goes through the same object.

- [ ] **Step 1: Failing adapter tests.** In `backend/tests/unit/test_llm_clients.py` (read it first — it drives the adapters through `httpx.MockTransport`), change every `embed` assertion from `vectors = await client.embed([...])` / `assert vectors == [...]` to:

```python
    result = await client.embed(["a", "b"])
    assert result.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert result.model == "nomic-embed-text"   # whatever embed_model the fixture passes
    assert result.usage.completion_tokens == 0
    assert result.usage.prompt_tokens == 7       # the mock body must include prompt_eval_count (Ollama) / usage.prompt_tokens (OpenAI) = 7
    assert result.usage.latency_ms >= 0
```

Add one case per adapter where the token count is absent from the body → `prompt_tokens == 0`. Add to `FakeLLM` tests (if any) / a new unit test: `(await FakeLLM().embed(["x"])).usage == Usage(prompt_tokens=1, completion_tokens=0, latency_ms=0.0)` and `.model == "fake-embed"`.

- [ ] **Step 2: Run to verify failure.** `python -m uv run pytest tests/unit/test_llm_clients.py -q` → FAIL: `list` has no attribute `vectors`.

- [ ] **Step 3: Implement the adapters.** `base.py`: add `EmbeddingResult` (above) and change the Protocol. `ollama.py`:

```python
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        start = time.perf_counter()
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.embed_model, "input": texts},
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama embed request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000
        if resp.is_error:
            raise LLMError(f"ollama embed failed: {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json()
            embeddings: list[list[float]] = data["embeddings"]
            prompt_tokens = int(data.get("prompt_eval_count", 0))
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"ollama embed returned an unexpected response shape: {exc}") from exc
        usage = Usage(prompt_tokens=prompt_tokens, completion_tokens=0, latency_ms=latency_ms)
        return EmbeddingResult(vectors=embeddings, usage=usage, model=self.embed_model)
```

`openai_compat.py`: same shape; token count from `data.get("usage", {}).get("prompt_tokens", 0)`; model from `data.get("model", self.embed_model)`. `FakeLLM.embed`:

```python
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        usage = Usage(prompt_tokens=len(texts), completion_tokens=0, latency_ms=0.0)
        return EmbeddingResult(
            vectors=[self._embed_one(text) for text in texts], usage=usage, model="fake-embed"
        )
```

and `self.embed_calls: list[list[str]] = []` in `__init__` (tests can count embed calls — Task 4 uses it).

- [ ] **Step 4: Run adapter tests.** → PASS. Then run `python -m uv run mypy app` — it will list every caller of `embed` still expecting a list (the two `retrieve.py` sites). Fix them in Step 5.

- [ ] **Step 5: Failing retriever test.** In the existing retriever unit test module (find it: `grep -rl "SchemaRetriever(" backend/tests/unit`), add:

```python
class _RecordingObserver(NullObserver):
    def __init__(self) -> None:
        self.llm_calls: list[tuple[str, str, Usage]] = []

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        self.llm_calls.append((stage, model, usage))


async def test_retriever_reports_embed_calls_to_the_observer(graph_fixture_name) -> None:
    observer = _RecordingObserver()
    retriever = SchemaRetriever(graph, FakeLLM(), top_k=4, observer=observer)   # match the real ctor kwargs
    await retriever.build_index()
    await retriever.retrieve("how many customers?")
    stages = [stage for stage, _, _ in observer.llm_calls]
    assert stages == ["embed", "embed"]          # one for the index, one for the question
    assert all(model == "fake-embed" for _, model, _ in observer.llm_calls)
    assert observer.llm_calls[0][2].prompt_tokens == len(graph.tables)
    assert observer.llm_calls[1][2].prompt_tokens == 1
```

(Adapt the fixture/ctor to what the module already uses; the assertions are the requirement.)

- [ ] **Step 6: Implement in core.** `retrieve.py`: `from app.core.observer import NullObserver, PipelineObserver`; constructor gains keyword-only `observer: PipelineObserver | None = None` → `self._observer: PipelineObserver = observer if observer is not None else NullObserver()`. Line 69 becomes:

```python
        embedded = await self._llm.embed(summaries)
        self._observer.on_llm(stage="embed", model=embedded.model, usage=embedded.usage)
        vectors = embedded.vectors
```

Line 157 becomes:

```python
        embedded = await self._llm.embed([question])
        self._observer.on_llm(stage="embed", model=embedded.model, usage=embedded.usage)
        [question_vec] = embedded.vectors
```

`schema_service.prepare_retriever`: add `*, llm_observer: PipelineObserver | None = None` and pass `observer=llm_observer` where `SchemaRetriever(...)` is constructed (both the cache-hit import path and the embed path construct/populate the retriever — make sure the observer is set on whichever object is returned). `bootstrap.py`: at both `prepare_retriever(...)` calls add `llm_observer=<the pipeline observer>` (in `refresh_components` that is `components.observer`; in `build_components` it is the resolved observer variable that is later stored in `AppComponents.observer` — read `_resolve_observers` to pick the right one).

- [ ] **Step 7: Metrics assertion.** In `backend/tests/integration/test_metrics_endpoint.py`, in the test that posts a query and reads `/metrics`, add:

```python
    assert 't2s_llm_request_duration_seconds_count{model="fake-embed",provider="ollama",stage="embed"}' in text
```

(Check the label order prometheus_client emits — alphabetical: `model, provider, stage`; and the `provider` value the test settings use.) Run: `python -m uv run pytest tests/integration/test_metrics_endpoint.py -q` → PASS.

- [ ] **Step 8: Docs.** ADR 0005 `:120-131`: replace the bullet with one stating `stage="embed"` is now emitted by `SchemaRetriever` via `EmbeddingResult.usage` (prompt tokens = embedded tokens where the API reports them, 0 otherwise; Ollama `/api/embed` reports `prompt_eval_count`, OpenAI `usage.prompt_tokens`), that embed usage is metrics-only and deliberately **not** folded into `PipelineOutput.usage`/`query_log.prompt_tokens`, and that `t2s_llm_cost_usd_total` prices embed models through the same table (0 unless listed). Dashboard: update the LLM-latency panel `description` to say the `embed` series is the question/index embedding. README Metrics section: mention `stage=generate|repair|embed` if it enumerates stages.

- [ ] **Step 9: Lint, full suite, commit.** `make lint`, `make test`. Commit `feat(observability): observe the embed LLM stage`.

---

### Task 3: Worker `/metrics` + worker container hardening

**Files:**
- Create: `backend/app/observability/exporter.py`, `backend/tests/unit/test_exporter.py`
- Modify: `backend/app/config.py` (after `queue_depth_sample_s`), `.env.example`, `backend/app/jobs/worker.py`
- Modify: `docker-compose.yml` (`worker` service), `deploy/prometheus/prometheus.yml`
- Modify docs: `docs/decisions/0004-async-jobs.md:95-98`, `docs/decisions/0005-observability.md:98-102,132-134`, `README.md` Metrics section, `docs/architecture.md:109-118`

**Interfaces:**
- Produces (`exporter.py`):

```python
@dataclass
class MetricsServer:
    port: int
    _httpd: WSGIServer
    _thread: threading.Thread

    def close(self) -> None:
        """Stop serving and join the thread (idempotent)."""


def start_metrics_server(registry: CollectorRegistry, *, port: int, addr: str = "0.0.0.0") -> MetricsServer:
    """Serve `registry` at http://addr:port/metrics on a daemon thread
    (prometheus_client.start_http_server). port=0 picks a free port — tests."""
```

- Produces: `Settings.worker_metrics_port: int = 9100` (`0` disables the worker exporter).
- Consumes: `AppComponents.metrics.registry` (`bootstrap.py`), `Settings.metrics_enabled`.

- [ ] **Step 1: Failing unit test.** `backend/tests/unit/test_exporter.py`:

```python
import httpx
from prometheus_client import CollectorRegistry, Counter

from app.observability.exporter import start_metrics_server


def test_metrics_server_serves_the_given_registry_and_closes() -> None:
    registry = CollectorRegistry()
    Counter("t2s_test_total", "test", registry=registry).inc(3)
    server = start_metrics_server(registry, port=0, addr="127.0.0.1")
    try:
        body = httpx.get(f"http://127.0.0.1:{server.port}/metrics", timeout=5).text
        assert "t2s_test_total 3.0" in body
    finally:
        server.close()
        server.close()  # idempotent
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{server.port}/metrics", timeout=1)
```

- [ ] **Step 2: Run to verify failure.** `python -m uv run pytest tests/unit/test_exporter.py -q` → FAIL: module not found.

- [ ] **Step 3: Implement `exporter.py`.**

```python
"""Serve a CollectorRegistry over HTTP from a process that has no ASGI app
-- the arq worker. app.main exposes the API's registry through
prometheus-fastapi-instrumentator; this is the same exposition format on
a plain WSGI thread, so Prometheus can scrape the worker too (ADR 0005)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from wsgiref.simple_server import WSGIServer

from prometheus_client import CollectorRegistry, start_http_server

__all__ = ["MetricsServer", "start_metrics_server"]


@dataclass
class MetricsServer:
    port: int
    _httpd: WSGIServer
    _thread: threading.Thread
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def start_metrics_server(
    registry: CollectorRegistry, *, port: int, addr: str = "0.0.0.0"
) -> MetricsServer:
    httpd, thread = start_http_server(port, addr=addr, registry=registry)
    return MetricsServer(port=httpd.server_port, _httpd=httpd, _thread=thread)
```

(prometheus_client ≥0.17 returns `(httpd, thread)`; if mypy complains about the return annotation of `start_http_server`, cast — do not `type: ignore` without a comment naming the stub gap.)

- [ ] **Step 4: Run unit test.** → PASS.

- [ ] **Step 5: Settings + worker wiring.** `config.py` after `queue_depth_sample_s`:

```python
    #: Port the arq worker serves its own Prometheus registry on (see
    #: app.observability.exporter and app.jobs.worker); scraped as the
    #: text2sql-worker job in deploy/prometheus/prometheus.yml. 0 disables
    #: the worker exporter. Ignored when metrics_enabled is false.
    worker_metrics_port: int = 9100
```

`.env.example`: `WORKER_METRICS_PORT=9100` with a one-line comment. `worker.py` `startup`/`shutdown`:

```python
async def startup(ctx: dict[Any, Any]) -> None:
    configure_logging(_settings.log_level)
    components = await build_components(_settings)
    ctx["components"] = components
    if _settings.metrics_enabled and _settings.worker_metrics_port > 0:
        ctx["metrics_server"] = start_metrics_server(
            components.metrics.registry, port=_settings.worker_metrics_port
        )


async def shutdown(ctx: dict[Any, Any]) -> None:
    server = ctx.pop("metrics_server", None)
    if server is not None:
        server.close()
    components = ctx.pop("components", None)
    if components is not None:
        await close_components(components)
```

Update the module docstring: the worker now serves its registry at `:worker_metrics_port/metrics`; the API's queue-depth gauge remains API-side. Add a unit test in `backend/tests/unit/test_jobs_tasks.py` (or a new `test_worker.py`) that calls `startup(ctx)`/`shutdown(ctx)` with `build_components` monkeypatched to return a `SimpleNamespace(metrics=Metrics())` and `_settings` patched to `worker_metrics_port=0` vs a free port, asserting `metrics_server` is absent vs present and closed after shutdown — keep it to the wiring, no containers.

- [ ] **Step 6: Compose + Prometheus.** `docker-compose.yml` `worker` service: replace `healthcheck: disable: true` with

```yaml
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:9100/metrics"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 30s
    restart: unless-stopped
    ports:
      - "127.0.0.1:9100:9100"
```

(confirm `curl` exists in the image — the backend HEALTHCHECK already uses it), add `WORKER_METRICS_PORT: "9100"` to its `environment`, and add `backend: condition: service_healthy` to its `depends_on` (the API's lifespan runs Alembic; starting the worker after it removes the first-job-before-migration race noted in the final review). Update the service's comment. `deploy/prometheus/prometheus.yml`: add

```yaml
  - job_name: text2sql-worker
    metrics_path: /metrics
    static_configs:
      - targets: ["worker:9100"]
```

and fix the header comment ("One job" → two). Verify: `docker compose config` valid. If Docker time allows, `docker compose up -d --build backend worker prometheus redis postgres`, then `curl -s 127.0.0.1:9100/metrics | grep -c t2s_` > 0 and `curl -s 127.0.0.1:9090/api/v1/targets | grep -o '"health":"up"' | wc -l` == 2; `docker compose down` (no `-v`). Report exactly what was verified.

- [ ] **Step 7: Docs.** ADR 0004 `:95-98`: worker metrics are now scraped (`text2sql-worker` job, port `WORKER_METRICS_PORT`); note the worker's `t2s_*` series carry Prometheus's `job="text2sql-worker"` label, so the dashboard's `sum(...)` panels aggregate both processes and per-process views are `by (job)`. ADR 0005 `:98-102` (push rejected — "a follow-up if job-level metrics start to matter" → "done: app/observability/exporter.py") and `:132-134` (delete the unscraped bullet). README Metrics: add the worker endpoint. `docs/architecture.md:109-118`: add the worker → Prometheus arrow to the chain.

- [ ] **Step 8: Lint, full suite, commit.** Commit `feat(jobs): serve worker metrics; healthcheck, restart policy, ordered start`.

---

### Task 4: Cross-process schema refresh (Redis epoch)

**Files:**
- Modify: `backend/app/cache/schema_cache.py` (add `get_epoch`/`bump_epoch`), `backend/app/cache/keys.py` (add `SCHEMA_EPOCH_KEY = "schema:epoch"`)
- Create: `backend/app/services/schema_sync.py`, `backend/tests/integration/test_schema_sync.py`
- Modify: `backend/app/services/bootstrap.py` (`AppComponents.schema_epoch: str | None`; baseline read in `build_components`), `backend/app/api/v1/schema.py:66-84`, `backend/app/jobs/tasks.py:81-88`, `backend/app/main.py` lifespan, `backend/app/jobs/worker.py`, `backend/app/config.py`, `.env.example`
- Modify docs: `docs/decisions/0004-async-jobs.md:88-94`, `README.md:298-305` (remove the last "Known gaps" bullet and the heading), `docs/architecture.md:100-107`
- Test: also `backend/tests/integration/test_jobs.py` (existing refresh-job test, if any, still passes)

**Interfaces:**
- Produces (`schema_cache.py`):

```python
    async def get_epoch(self) -> str | None:
        """The current schema epoch (an opaque token written by bump_epoch),
        or None if none was ever written or Redis is unavailable/disabled."""

    async def bump_epoch(self) -> str | None:
        """Write a fresh epoch token (uuid4 hex) and return it, or None if the
        write failed (Redis down/disabled) -- the caller keeps its own
        components; other processes simply will not learn about this refresh."""
```

Stored via `RedisCache.set_json(SCHEMA_EPOCH_KEY, token, ttl_s=self._ttl_s)` (same TTL the schema index uses) and read via `get_json`. `invalidate_all()` must **not** delete the epoch key (it deletes `schema:` entries by prefix — exclude `schema:epoch`, e.g. re-set it after the prefix delete, or delete only `schema:` keys other than the epoch; write a unit/integration assertion for this).

- Produces (`schema_sync.py`):

```python
async def mark_refreshed(components: AppComponents) -> None:
    """Called by whoever just ran refresh_components: bump the shared epoch
    and remember it as this process's own, so the watcher does not refresh
    again for its own bump."""
    components.schema_epoch = await components.schema_cache.bump_epoch() or components.schema_epoch


async def sync_schema_if_stale(components: AppComponents) -> bool:
    """One watcher iteration: if the shared epoch differs from this
    process's, run refresh_components and adopt it. Returns True if a
    refresh happened. Never raises for Redis errors (get_epoch returns None
    -> no-op); a failing refresh is logged and re-attempted next tick."""


async def watch_schema_epoch(
    components: AppComponents, *, period_s: float, on_refreshed: Callable[[], None] | None = None
) -> None:
    """Loop sync_schema_if_stale every period_s seconds forever; the owner
    cancels the task. on_refreshed runs after a successful refresh (the API
    passes a closure that calls publish_components(app.state, components))."""
```

- Produces: `Settings.schema_sync_poll_s: float = 5.0` (`0` disables the watcher). `AppComponents.schema_epoch: str | None` (baseline = `await schema_cache.get_epoch()` at the end of `build_components`, so a process that starts after an old refresh does not refresh again for it).
- Consumes: `refresh_components` (unchanged), `publish_components`, `RedisCache.get_json/set_json/delete_prefix`.

- [ ] **Step 1: Failing integration test.** `backend/tests/integration/test_schema_sync.py` (copy the `settings` fixture shape from `test_jobs.py`, `queue_depth_sample_s` not needed):

```python
async def test_refresh_in_one_runtime_propagates_to_another(settings: Settings) -> None:
    a = await build_components(settings, llm=FakeLLM())
    b = await build_components(settings, llm=FakeLLM())
    try:
        old_graph_b = b.graph
        assert await sync_schema_if_stale(b) is False          # nothing happened yet

        await refresh_components(a)
        await mark_refreshed(a)
        assert a.schema_epoch is not None

        assert await sync_schema_if_stale(b) is True
        assert b.schema_epoch == a.schema_epoch
        assert b.graph is not old_graph_b                        # rebuilt
        assert await sync_schema_if_stale(b) is False            # idempotent
        assert await sync_schema_if_stale(a) is False            # the refresher does not re-refresh for its own bump
    finally:
        await close_components(a)
        await close_components(b)


async def test_sync_is_a_noop_without_redis(settings: Settings) -> None:
    dead = settings.model_copy(update={"redis_url": "redis://localhost:1"})
    c = await build_components(dead, llm=FakeLLM())
    try:
        assert c.schema_epoch is None
        assert await sync_schema_if_stale(c) is False
        await mark_refreshed(c)                                  # must not raise
    finally:
        await close_components(c)


async def test_invalidate_all_keeps_the_epoch(settings: Settings) -> None:
    c = await build_components(settings, llm=FakeLLM())
    try:
        await mark_refreshed(c)
        await c.schema_cache.invalidate_all()
        assert await c.schema_cache.get_epoch() == c.schema_epoch
    finally:
        await close_components(c)


async def test_post_schema_refresh_reaches_a_second_api_process(settings: Settings) -> None:
    fast = settings.model_copy(update={"schema_sync_poll_s": 0.2})
    app_a = create_app(settings=fast, llm=FakeLLM())
    app_b = create_app(settings=fast, llm=FakeLLM())
    async with LifespanManager(app_a), LifespanManager(app_b):
        b_before = app_b.state.components.graph
        async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            assert (await client.post("/api/v1/schema/refresh")).status_code == 200
        for _ in range(50):                                      # ≤ 5 s
            if app_b.state.components.graph is not b_before:
                break
            await asyncio.sleep(0.1)
        assert app_b.state.components.graph is not b_before
        assert app_b.state.graph is app_b.state.components.graph  # publish_components ran
```

(Two apps share the same Redis container and target DB; the schema is unchanged so the version is equal — identity of the graph object is the signal.)

- [ ] **Step 2: Run to verify failure.** `python -m uv run pytest tests/integration/test_schema_sync.py -q` → FAIL: import errors.

- [ ] **Step 3: Implement.** `keys.py`: `SCHEMA_EPOCH_KEY = "schema:epoch"` (add to `__all__`). `schema_cache.py`: the two methods above (`uuid.uuid4().hex` token); make `invalidate_all` preserve the epoch (read it first, `delete_prefix("schema:")`, re-set it if it was present — or delete individual index keys; pick what fits the existing implementation and keep the test in Step 1 green). `bootstrap.py`: add `schema_epoch: str | None` to `AppComponents` (after `schema_index_source`) and set `schema_epoch=await schema_cache.get_epoch()` when constructing it in `build_components`. `schema_sync.py`:

```python
"""Propagate a schema refresh from the process that ran it to every other
process sharing the Redis (API replicas, the arq worker) -- ADR 0004.

Mechanism: an opaque epoch token under schema:epoch. mark_refreshed() bumps
it after refresh_components(); watch_schema_epoch() polls it and, on a
change, runs refresh_components() locally. Redis down or disabled degrades
to Phase 2 behaviour (per-process refresh only), never to an error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.services.bootstrap import AppComponents, refresh_components

__all__ = ["mark_refreshed", "sync_schema_if_stale", "watch_schema_epoch"]

logger = logging.getLogger(__name__)


async def mark_refreshed(components: AppComponents) -> None:
    epoch = await components.schema_cache.bump_epoch()
    if epoch is not None:
        components.schema_epoch = epoch


async def sync_schema_if_stale(components: AppComponents) -> bool:
    epoch = await components.schema_cache.get_epoch()
    if epoch is None or epoch == components.schema_epoch:
        return False
    try:
        graph = await refresh_components(components)
    except Exception:  # noqa: BLE001 -- keep serving the old schema; retry next tick
        logger.exception("schema sync: refresh failed, keeping current schema")
        return False
    components.schema_epoch = epoch
    logger.info("schema sync: refreshed", extra={"schema_version": graph.version})
    return True


async def watch_schema_epoch(
    components: AppComponents,
    *,
    period_s: float,
    on_refreshed: Callable[[], None] | None = None,
) -> None:
    while True:
        if await sync_schema_if_stale(components) and on_refreshed is not None:
            on_refreshed()
        await asyncio.sleep(period_s)
```

`api/v1/schema.py` `refresh_schema`: after `refresh_components`, `await mark_refreshed(state.components)` then `publish_components(...)`. `jobs/tasks.py` `refresh_schema_job`: `graph = await refresh_components(components); await mark_refreshed(components)`. `config.py`:

```python
    #: How often (seconds) each process checks the shared schema epoch in
    #: Redis and re-introspects if another process refreshed the schema --
    #: see app.services.schema_sync. 0 disables the watcher.
    schema_sync_poll_s: float = 5.0
```

`.env.example`: `SCHEMA_SYNC_POLL_S=5.0`. `main.py` lifespan, after the sampler block:

```python
        watcher: asyncio.Task[None] | None = None
        if resolved_settings.schema_sync_poll_s > 0:
            watcher = asyncio.create_task(
                watch_schema_epoch(
                    components,
                    period_s=resolved_settings.schema_sync_poll_s,
                    on_refreshed=lambda: publish_components(app.state, components),
                )
            )
            watcher.add_done_callback(_log_task_exit("schema epoch watcher"))
```

Generalise `_log_sampler_exit` into `_log_task_exit(name) -> Callable[[asyncio.Task[None]], None]` and use it for both tasks; cancel/await the watcher in the `finally` like the sampler. `worker.py` startup: `if _settings.schema_sync_poll_s > 0: ctx["schema_watch"] = asyncio.create_task(watch_schema_epoch(components, period_s=_settings.schema_sync_poll_s))`; shutdown cancels it (`task.cancel()`, `with suppress(asyncio.CancelledError): await task`).

- [ ] **Step 4: Run the new tests + neighbours.** `python -m uv run pytest tests/integration/test_schema_sync.py tests/integration/test_jobs.py tests/integration/test_schema*.py -q` → PASS.

- [ ] **Step 5: Docs.** ADR 0004 `:88-94`: replace with the epoch mechanism (who bumps, who polls, poll period setting, degradation when Redis is down, why polling over pub/sub: no extra connection type in `RedisCache`, bounded staleness of `schema_sync_poll_s`, and it also covers a process that missed a pub/sub message while restarting). Note PLAN.md item 3's "schema refresh also becomes a job" is satisfied by `refresh_schema_job` + propagation — no HTTP trigger for the job is needed since `POST /schema/refresh` now reaches the worker. `README.md`: remove the refresh bullet and, now empty, the "Known gaps" paragraph. `docs/architecture.md:100-107`: one sentence on propagation.

- [ ] **Step 6: Lint, full suite, commit.** Commit `feat(schema): propagate schema refresh across processes via a redis epoch`.

---

### Task 5: `GET /jobs/{id}` for non-query jobs

**Files:**
- Modify: `backend/app/api/v1/jobs.py:100-119`
- Test: `backend/tests/integration/test_jobs.py`

**Interfaces:**
- Consumes: `arq.jobs.JobResult.function: str`.
- Produces: for a completed job whose `info.function != "run_query_job"`, `JobStatusResponse(status="complete", result=None, error=None)` — the job finished, but it has no `QueryResponse` to show. Frontend types already allow `result: null`.

- [ ] **Step 1: Failing test.** In `test_jobs.py`:

```python
async def test_get_job_on_a_schema_refresh_job_reports_complete_without_a_result(
    settings: Settings,
) -> None:
    async with _api(settings, FakeLLM([])) as client:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            job = await pool.enqueue_job("refresh_schema_job")
            assert job is not None
        finally:
            await pool.aclose()
        await _drain_queue(settings, FakeLLM([]))
        done = await client.get(f"/api/v1/jobs/{job.job_id}")
    assert done.status_code == 200
    assert done.json() == {"job_id": job.job_id, "status": "complete", "result": None, "error": None}
```

- [ ] **Step 2: Run to verify failure.** → FAIL with 500 (`ValidationError` building `QueryResponse(**{"version": ...})`).

- [ ] **Step 3: Implement.** In `get_job`, before the final return:

```python
    if info.function != run_query_job.__name__:
        # A job that does not produce a QueryResponse (refresh_schema_job):
        # it finished, there is just nothing to render.
        return JobStatusResponse(job_id=job_id, status="complete")
```

Import `run_query_job` from `app.jobs.tasks` and also use `run_query_job.__name__` in `enqueue_job(...)` in place of the `"run_query_job"` string literal (removes the rename coupling the final review noted). Update the `JobStatusResponse.result` docstring: "None until it completes, for a failed job, and for a completed job of a kind that has no QueryResponse (a schema refresh)."

- [ ] **Step 4: Run tests.** `python -m uv run pytest tests/integration/test_jobs.py -q` → PASS.

- [ ] **Step 5: Lint, full suite, commit.** Commit `fix(jobs): report non-query jobs as complete without a result`.

---

## Self-review

- **Spec coverage:** ADR 0003 gap (query_log) → T1. PLAN item 4 `stage=embed` / ADR 0005 embed bullet → T2. ADR 0004/0005 worker-unscraped + worker restart/ordering → T3. ADR 0004 per-process refresh + README known gap + PLAN item 3 → T4. Final-review item 13 (`GET /jobs/{id}` 500) + string-literal coupling → T5. ✔
- **Placeholders:** none — every step has code or an exact edit; the two "adapt to the module's fixture" notes point at concrete existing tests the implementer must read.
- **Type consistency:** `EmbeddingResult(vectors, usage, model)` used identically in T2 adapters/FakeLLM/retriever; `AppComponents.schema_epoch: str | None` set in T4 bootstrap and read in `schema_sync`; `MetricsServer.close()` used by worker shutdown in T3; `run_query_job.__name__` in T5 matches `functions=[run_query_job, ...]`.
- **Constraint check:** `retrieve.py` imports only `app.core.observer` (core); `exporter.py` lives in `app/observability` (app layer); `schema_sync.py` in `app/services`. No new deps. Embed usage never touches `PipelineOutput.usage`.
