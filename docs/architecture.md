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
        Guard-->>API: GuardError(reason)
        API->>AppDB: record_query(error="guard:reason")
        API-->>Frontend: 200 {error: "guard:reason", rows: []}
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
