# Architecture

## Request path

Sequence for `POST /api/v1/query`, including the optional one-shot repair.

```mermaid
sequenceDiagram
    participant Browser
    participant Frontend as nginx / frontend
    participant API as FastAPI (/api/v1/query)
    participant Retriever as SchemaRetriever
    participant LLM as LLM provider
    participant Builder as PromptBuilder
    participant Guard as guard_sql
    participant Executor as execute_readonly
    participant Target as target_db (readonly role)
    participant AppDB as app_db (query_log)

    Browser->>Frontend: POST /api/question
    Frontend->>API: POST /api/v1/query {question}
    API->>Retriever: retrieve(question)
    Retriever->>LLM: embed(question)
    LLM-->>Retriever: vector
    Retriever-->>API: top-k tables + FK-hop neighbours
    API->>Builder: generate(question, ddl)
    Builder-->>API: system/user prompt
    API->>LLM: complete(system, user)
    LLM-->>API: {sql, explanation}
    API->>Guard: guard_sql(sql, known_tables, max_rows)
    alt policy violation
        Guard-->>API: GuardError(reason, detail)
        API->>AppDB: record_query(error="guard:reason: detail")
        API-->>Frontend: 200 {error: "guard:reason: detail", rows: []}
    else accepted
        Guard-->>API: guarded SQL (LIMIT applied)
        API->>Executor: execute_readonly(sql)
        Executor->>Target: SET TRANSACTION READ ONLY; run; rollback
        alt success
            Target-->>Executor: rows
            Executor-->>API: QueryResult
        else recoverable DB error (syntax/other)
            Target-->>Executor: error
            Executor-->>API: ExecutionError(kind)
            API->>Builder: repair(question, ddl, bad_sql, error)
            Builder-->>API: repair prompt
            API->>LLM: complete(system, user)
            LLM-->>API: {sql, explanation}
            API->>Guard: guard_sql(...)
            API->>Executor: execute_readonly(...) (second attempt)
            Executor->>Target: run; rollback
            Target-->>Executor: rows or terminal error
            Executor-->>API: QueryResult or ExecutionError
        end
        API->>AppDB: record_query(...)
        API-->>Frontend: 200 {sql, rows, columns, tables, timings, usage, error}
    end
    Frontend-->>Browser: render SQL panel + results table
```

Every branch returns `HTTP 200` — a pipeline failure (guard rejection,
unrecoverable execution error, LLM transport error, unparsable output) is
reported in the response body's `error` field, never as a non-2xx status;
see `backend/app/core/pipeline.py`'s module docstring for the exact
terminal-vs-repairable distinction. The exceptions are request validation
(`422` on an empty/too-long question) and a `503` from `GET /readyz`/the
`get_pipeline` dependency while the schema index hasn't finished loading.

## Phase 2 additions

Phase 1's request path above is still the whole story for a cold question.
Phase 2 wraps three caches, a job queue and a metrics pipeline around it —
each designed in its own ADR ([0003 cache](decisions/0003-cache-design.md),
[0004 async jobs](decisions/0004-async-jobs.md),
[0005 observability](decisions/0005-observability.md)); the summary here is
only how the pieces sit relative to the diagram.

**Cache tiers.** All three live in Redis behind `app/cache/redis.py`, which
swallows every Redis error and timeout: with Redis down the API answers
exactly as it did in Phase 1, only slower.

| Tier | Key | Populated | Read |
|---|---|---|---|
| Schema index | `schema:{graph.version}` | startup / `POST /schema/refresh`, after embedding every table summary | startup, to skip the embed pass entirely |
| SQL | `sql:{sha256(schema_version\|model\|prompt_version\|normalised_question)[:32]}` | after a successful run | before `retrieve`; a hit skips retrieve→generate but **re-runs the guard and the executor** |
| Result | `res:{same digest}` | after a successful run, `use_result_cache=True` only | before the SQL tier, opt-in; a hit skips the target DB too |

Baking `schema_version`/`model`/`prompt_version` into the digest is what
makes invalidation implicit: a schema refresh, a model swap or a prompt
bump simply stops addressing the old entries. Explicit eviction is narrow —
a SQL-tier hit that the guard now rejects deletes both tiers for that
digest; an execution failure on a cached statement evicts only when the
error looks like the *statement's* fault (`app/core/pipeline.py`'s
`_should_evict`). Errors are never cached, at any tier, and a result larger
than `RESULT_CACHE_MAX_BYTES` is silently not stored.
`PipelineOutput.cache_status` reports which path the request took:
`disabled` (`CACHE_ENABLED=false`), `bypass` (`use_cache=false` on the
request), `miss`, `sql_hit`, `result_hit`.

**Async jobs.** `POST /api/v1/query/async` enqueues onto arq (Redis) and
returns `202 {job_id}`; `GET /api/v1/jobs/{job_id}` polls it. The worker
(`app/jobs/worker.py`) builds its runtime with the very same
`app.services.bootstrap.build_components()` the API lifespan uses, so a job
runs the identical pipeline, caches and schema version — and writes the
same `query_log` row, carrying the enqueueing request's `request_id`
through. A dead queue degrades only the async endpoints (503); the
synchronous route is unaffected.

**Metrics.** The pipeline reports events to a `PipelineObserver` protocol
defined in `app/core/observer.py`, which imports nothing framework-shaped.
The app layer's `MetricsObserver` (`app/observability/metrics.py`) turns
those into `t2s_*` collectors on a per-app `CollectorRegistry`, served at
`GET /metrics` and scraped by Prometheus into the Grafana dashboard under
`deploy/`. The core never learns that Prometheus exists.

```
pipeline → PipelineObserver → MetricsObserver → /metrics → Prometheus → Grafana
```

## Components

| Module | Responsibility |
|---|---|
| `frontend/` (Vite + React + nginx) | Single-page UI: question box, SQL panel, results table, schema sidebar, error display. `nginx.conf` proxies `/api/`, `/healthz`, `/readyz` to the backend. |
| `backend/app/main.py` | FastAPI app factory; lifespan runs app-DB migrations, opens both engines, builds/injects the LLM client, introspects the target schema, builds the retrieval index (with a startup timeout), assembles the `Text2SQLPipeline`, and stores everything on `app.state`. |
| `backend/app/api/v1/query.py` | `POST /api/v1/query` — runs the pipeline, logs the result, maps `PipelineOutput` to the response schema. Always 200 except validation/readiness. |
| `backend/app/api/v1/schema.py` | `GET /api/v1/schema` (current `SchemaGraph` as JSON) and `POST /api/v1/schema/refresh` (re-introspect, rebuild the retriever, atomically swap in a fresh pipeline). |
| `backend/app/api/v1/health.py` | `/healthz` (liveness) and `/readyz` (readiness — schema loaded + both DBs answer `SELECT 1`). |
| `backend/app/core/pipeline.py` | Orchestrates retrieve → render → generate → parse → guard → execute, plus the single repair attempt. Framework-free. |
| `backend/app/core/schema/introspect.py` | SQLAlchemy `Inspector` → `SchemaGraph` (tables, columns, FKs, comments), with a content hash as `graph.version`. |
| `backend/app/core/schema/retrieve.py` | `SchemaRetriever`: one embedding per table summary, cosine top-k plus a keyword boost for table/column names mentioned verbatim in the question, then FK-hop expansion. Lazily self-heals if the startup embed attempt failed. |
| `backend/app/core/schema/render.py` | Renders a `SchemaGraph` subset into compact DDL-ish prompt text. |
| `backend/app/core/prompting/builder.py` | Jinja2 `generate`/`repair` prompt construction (with few-shot examples) and `parse_llm_output`, which recovers `(sql, explanation)` from the several shapes an LLM tends to actually return. |
| `backend/app/core/sql/guard.py` | `sqlglot`-based AST policy: SELECT-only, denylisted functions (exact + prefix families), no writes/`INTO`/row locks, every table known, `LIMIT` injection/clamping. The UX layer, not the security boundary. |
| `backend/app/core/sql/executor.py` | `execute_readonly`: opens a connection, forces `SET TRANSACTION READ ONLY` and a `statement_timeout` itself, fetches at most `max_rows + 1` rows to detect truncation, always rolls back. The actual security boundary, alongside the Postgres `readonly` role. |
| `backend/app/llm/` | `LLMClient` protocol (`complete`, `embed`) plus `OllamaClient` and `OpenAICompatClient` adapters, selected by `LLM_PROVIDER` in `factory.py`. |
| `backend/app/db/` | `session.py` (separate async engines for `app_db` and `target_db`); `models.py`/`query_log.py` (the `QueryLog` ORM model and the one-row-per-run writer). |
| `backend/alembic/` | Migrations for `app_db`'s own tables (currently just `query_log`). |
| `seed/` | `schema.sql` (NorthwindNext DDL + `COMMENT`s), `generate.py` (seeded Faker generator), `roles.sql`-equivalent role setup, `questions.yaml` (the eval set), and the init container that loads all of it into the `postgres` service on first boot. |
| `scripts/eval.py` | Runs every question in `seed/questions.yaml` through a live API, reports executable/row-match percentages — an eval, not a CI gate (LLM output is non-deterministic). |
| `docs/decisions/` | ADRs for the design decisions in `PLAN.md` §4, written up as they're implemented. |
