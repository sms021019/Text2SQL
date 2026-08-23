# Text2SQL — Cloud-Native NL→SQL Assistant: Project Plan

> Status: **DRAFT for review** — nothing scaffolded yet.
> Date: 2026-08-23

## 0. One-paragraph pitch

An open-source service that turns a natural-language question into a safe, read-only SQL query against a Postgres database, executes it, and returns results — built to showcase production infrastructure skills: containerised services, Kubernetes deployment, CI/CD, Redis caching, async job processing, Prometheus/Grafana observability, and Terraform-managed cloud infra. The LLM is pluggable (Ollama locally, any OpenAI-compatible API in the cloud) so the whole thing runs for free on a laptop with `docker compose up`.

Guiding principle: **the backend and the infra are the product.** The frontend exists so a reviewer can click a button, and every phase ends in a state you can demo in under two minutes.

---

## 1. Repository structure

Monorepo. One `git clone`, one `docker compose up`.

```
text2sql/
├── README.md                      # demo GIF, quickstart, architecture diagram
├── PLAN.md                        # this file
├── LICENSE                        # MIT
├── Makefile                       # make up / test / lint / seed / load-test
├── docker-compose.yml             # full local stack
├── docker-compose.override.yml    # dev: hot reload, exposed ports
├── .env.example
├── .pre-commit-config.yaml        # ruff, mypy, hadolint, terraform fmt
│
├── backend/
│   ├── pyproject.toml             # uv-managed; ruff + mypy config
│   ├── Dockerfile                 # multi-stage, non-root, uv
│   ├── alembic/                   # migrations for the APP's own tables (query log, feedback)
│   ├── app/
│   │   ├── main.py                # FastAPI app factory, lifespan, middleware
│   │   ├── config.py              # pydantic-settings; one Settings object
│   │   ├── api/
│   │   │   ├── router.py
│   │   │   ├── v1/
│   │   │   │   ├── query.py       # POST /query (sync), POST /query/async, GET /jobs/{id}
│   │   │   │   ├── schema.py      # GET /schema, POST /schema/refresh
│   │   │   │   └── health.py      # /healthz /readyz
│   │   │   └── deps.py            # DI providers: db session, redis, llm client
│   │   ├── core/                  # pure domain logic — no FastAPI imports
│   │   │   ├── pipeline.py        # orchestrates: retrieve → prompt → generate → validate → execute
│   │   │   ├── schema/
│   │   │   │   ├── introspect.py  # SQLAlchemy Inspector → SchemaGraph (tables, cols, FKs, comments)
│   │   │   │   ├── models.py      # Table, Column, ForeignKey pydantic models
│   │   │   │   ├── embed.py       # embed table/column descriptions
│   │   │   │   ├── retrieve.py    # top-k tables for a question (+ FK neighbourhood expansion)
│   │   │   │   └── render.py      # SchemaGraph subset → DDL-ish prompt text
│   │   │   ├── sql/
│   │   │   │   ├── guard.py       # sqlglot AST policy: SELECT-only, no CTE writes, LIMIT injection
│   │   │   │   ├── executor.py    # runs on read-only role, statement_timeout, row cap
│   │   │   │   └── repair.py      # one-shot self-correction loop on DB error
│   │   │   ├── prompting/
│   │   │   │   ├── templates/     # jinja2 prompt files, versioned
│   │   │   │   └── builder.py
│   │   │   └── errors.py          # typed domain exceptions
│   │   ├── llm/
│   │   │   ├── base.py            # Protocol: complete(), embed(); returns usage + latency
│   │   │   ├── openai_compat.py   # OpenAI / Groq / Together / vLLM / LM Studio
│   │   │   ├── ollama.py
│   │   │   └── factory.py         # LLM_PROVIDER env var → client
│   │   ├── cache/
│   │   │   ├── redis.py           # connection, key helpers, namespacing
│   │   │   ├── result_cache.py    # (schema_version, normalised question) → result
│   │   │   └── schema_cache.py    # serialised SchemaGraph + embeddings
│   │   ├── jobs/
│   │   │   ├── worker.py          # arq WorkerSettings
│   │   │   └── tasks.py           # run_query_job, refresh_schema_job
│   │   ├── observability/
│   │   │   ├── metrics.py         # prometheus_client registry + all custom metrics
│   │   │   ├── logging.py         # structlog JSON config, request-id binding
│   │   │   └── middleware.py      # request timing, correlation ids
│   │   └── db/
│   │       ├── session.py         # async engine for app DB + separate read-only engine for target DB
│   │       └── models.py          # QueryLog, Feedback ORM models
│   └── tests/
│       ├── conftest.py            # testcontainers: postgres + redis fixtures (session-scoped)
│       ├── unit/                  # guard, retrieve, render, prompt builder — no containers
│       ├── integration/           # pipeline against real Postgres + fake LLM
│       └── fakes/                 # FakeLLM returning canned SQL; deterministic embeddings
│
├── frontend/
│   ├── Dockerfile                 # build → nginx static
│   ├── package.json               # Vite + React + TS
│   └── src/
│       ├── App.tsx                # one page: question box, SQL panel, result table, "explain" toggle
│       ├── api.ts                 # typed fetch client (generated from OpenAPI later)
│       └── components/
│
├── seed/
│   ├── schema.sql                 # e-commerce DDL with COMMENTs on tables/columns
│   ├── generate.py                # Faker-based deterministic generator (seeded RNG)
│   ├── Dockerfile                 # init container that loads schema + data
│   └── questions.yaml             # 30–50 (question, expected SQL/row-count) eval pairs
│
├── deploy/
│   ├── k8s/
│   │   ├── base/                  # kustomize base: deployments, services, configmaps, HPA
│   │   └── overlays/
│   │       ├── local-k3d/
│   │       └── cloud/
│   ├── helm/                      # (optional later) chart wrapping the kustomize base
│   ├── grafana/
│   │   ├── dashboards/text2sql.json
│   │   └── provisioning/
│   ├── prometheus/prometheus.yml
│   └── terraform/
│       ├── modules/               # vpc, eks (or lightsail/ec2-k3s), rds, elasticache, ecr
│       └── envs/dev/
│
├── scripts/
│   ├── eval.py                    # runs seed/questions.yaml through the API, reports accuracy
│   └── load_test.js               # k6 script to make the Grafana dashboard move
│
├── docs/
│   ├── architecture.md            # C4-ish diagrams (mermaid)
│   ├── decisions/                 # ADRs: 0001-sql-safety.md, 0002-schema-retrieval.md, ...
│   └── demo.md                    # exact demo script per phase
│
└── .github/
    └── workflows/
        ├── ci.yml                 # lint, typecheck, unit + integration tests, build images
        ├── release.yml            # tag → push images to GHCR with SBOM
        └── deploy.yml             # (Phase 4) apply manifests to cluster
```

**Why this shape**

- `core/` has zero framework imports so the pipeline is unit-testable and could be lifted into a CLI or worker unchanged.
- Two Postgres *roles* (and in compose, two databases): `app_db` for our own tables (query log, feedback) and `target_db` (the seed e-commerce data) accessed via a `readonly` role. Separation is itself a safety layer and an interview talking point.
- `deploy/` kept out of `backend/` so infra changes don't trigger app image rebuilds in CI path filters.
- ADRs in `docs/decisions/` — you write them as you go; they are the interview cheat-sheet.

---

## 2. Seed dataset: "NorthwindNext" e-commerce

A realistic but compact schema that forces joins, aggregates, dates and a couple of traps (soft deletes, status enums, money as `NUMERIC`).

| Table | Rows (approx) | Notes |
|---|---|---|
| `customers` | 2,000 | country, signup_date, segment |
| `addresses` | 2,500 | 1:N with customers |
| `categories` | 20 | self-referencing `parent_id` (hierarchy trap) |
| `products` | 500 | sku, category_id, unit_price, `discontinued_at` (soft delete) |
| `suppliers` | 40 | |
| `inventory` | 500 | per-product stock, reorder_level |
| `orders` | 20,000 | status enum (`pending/paid/shipped/cancelled/refunded`), order_date, shipping_address_id |
| `order_items` | 60,000 | quantity, unit_price snapshot (not product.unit_price — a classic trap) |
| `payments` | 20,000 | method, amount, captured_at |
| `shipments` | 18,000 | carrier, shipped_at, delivered_at |
| `reviews` | 8,000 | rating 1–5 |

- Every table and most columns carry a SQL `COMMENT` — introspection reads them into the prompt, which is the cheapest form of schema grounding.
- `seed/generate.py` uses a fixed RNG seed so row counts and eval answers are reproducible across machines.
- Data sized so `docker compose up` seeds in <30s and Postgres stays tiny, but big enough that an unbounded `SELECT *` without `LIMIT` is visibly bad — motivates LIMIT injection.
- `questions.yaml` becomes the regression/eval set: simple lookups, joins, aggregations, date ranges, "top N", and a few deliberately ambiguous ones.

---

## 3. Phased roadmap

Each phase ends with a tagged release (`v0.1`, `v0.2`, …), a `docs/demo.md` script, and a green CI run (from Phase 3 on).

### Phase 1 — Core NL→SQL API, local, `docker compose up` (tag `v0.1`)

**Goal:** Clone → `make up` → open `localhost:5173` → ask "top 5 products by revenue last quarter" → see SQL + rows.

Deliverables:
1. `docker-compose.yml`: `backend`, `frontend`, `postgres` (seeded via init container), `ollama` (profile-gated; pull `qwen2.5-coder:7b` or similar small model; `nomic-embed-text` for embeddings).
2. LLM client abstraction with `ollama` + `openai_compat` implementations; switch via `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`.
3. Schema introspection → `SchemaGraph` (in-memory, rebuilt on startup).
4. Retrieval v1: embed `"{table}: {comment}; columns: …"` per table; cosine top-k; expand by 1 FK hop. Embeddings stored in memory (Phase 2 moves them to Redis).
5. Prompt builder with jinja2 templates (few-shot examples from `questions.yaml`).
6. `guard.py`: sqlglot parse → reject anything but a single `SELECT`/`WITH … SELECT`; reject functions on a denylist (`pg_sleep`, `pg_read_file`, `dblink`, `set_config`, …); reject `INTO`/`FOR UPDATE`; inject `LIMIT` if absent; qualify that all referenced tables exist in the schema.
7. Executor: dedicated read-only connection pool (`default_transaction_read_only=on`, `statement_timeout=5s`, row cap).
8. One-shot repair: on DB error, re-prompt with the error message once.
9. `POST /api/v1/query` → `{sql, rows, columns, explanation, timings, tokens}`.
10. Query log table (question, sql, success, latency, tokens) via Alembic — this is the data source for later metrics and for the eval script.
11. Frontend: single page, question input, SQL (read-only code block), results table, error display.
12. Tests: unit tests for `guard` (the largest suite — every forbidden construct gets a case), `retrieve`, `render`; integration test with testcontainers Postgres + `FakeLLM` running the full pipeline against the seed schema.
13. `scripts/eval.py` + `questions.yaml` with a first accuracy number in the README (honest, even if it's 60%).

**Demo:** browser + `docker compose logs -f backend` showing structured JSON logs.

### Phase 2 — Caching, async queue, observability (tag `v0.2`)

**Goal:** Same demo, now with a Grafana dashboard moving under k6 load, a cache-hit indicator in the UI, and long queries running in the background.

Deliverables:
1. Redis added to compose. `schema_cache`: SchemaGraph + embeddings persisted with a `schema_version` hash (of the introspected DDL). `result_cache`: key = `sha256(schema_version + model + prompt_version + normalised_question)`, TTL configurable, bypass flag.
2. `POST /schema/refresh` invalidates schema cache (and therefore all result keys, since version is in the key).
3. arq worker service: `POST /query/async` → job id; `GET /jobs/{id}` polls; frontend shows a spinner. Sync endpoint stays for the simple demo. Schema refresh also becomes a job.
4. Prometheus metrics (`/metrics`):
   - `t2s_llm_request_duration_seconds{provider,model,stage=generate|embed|repair}` histogram
   - `t2s_llm_tokens_total{provider,model,direction=prompt|completion}` counter
   - `t2s_llm_cost_usd_total{provider,model}` counter (price table in config; 0 for Ollama)
   - `t2s_sql_guard_rejections_total{reason}`
   - `t2s_sql_execution_total{outcome=success|db_error|timeout|rejected}`
   - `t2s_sql_execution_duration_seconds`
   - `t2s_cache_requests_total{cache=result|schema,outcome=hit|miss}`
   - `t2s_pipeline_duration_seconds` end-to-end
   - `t2s_jobs_queue_depth` gauge (from arq)
   - plus standard FastAPI request metrics via `prometheus-fastapi-instrumentator`.
5. Prometheus + Grafana in compose with a provisioned dashboard JSON (committed): p50/p95 LLM latency, SQL success rate, cache hit rate, token spend, queue depth.
6. `structlog` JSON logs with `request_id`/`job_id` correlation; exemplars linking from Prometheus histograms to request ids (nice-to-have).
7. `scripts/load_test.js` (k6) replaying `questions.yaml` to make the dashboard live.
8. Tests: testcontainers Redis fixture; cache key/invalidation unit tests; arq job integration test.

**Demo:** run k6, open Grafana, toggle cache off and watch hit rate drop and LLM latency rise.

### Phase 3 — CI/CD (tag `v0.3`)

**Goal:** Every PR runs lint/typecheck/tests with real containers; every tag publishes multi-arch images.

Deliverables:
1. `ci.yml`: matrix job — `ruff`, `mypy --strict` on `core/`, `pytest` (testcontainers needs Docker-in-runner — GitHub-hosted ubuntu runners have it), frontend `tsc` + `vitest`, `hadolint`, `docker build` (no push) with layer cache.
2. Path filters so `deploy/` changes don't rebuild images and vice versa.
3. `release.yml`: on `v*` tag → build `linux/amd64,arm64` → push to GHCR → attach SBOM (`syft`) + Trivy scan gate.
4. `pre-commit` config mirroring CI so failures surface locally.
5. Dependabot/Renovate for Python + npm + GitHub Actions.
6. Branch protection + required checks (documented in README; you set it in repo settings).
7. Coverage report as a PR comment; badges in README.

**Demo:** open a PR, show checks; cut a tag, show the GHCR package page.

### Phase 4 — Kubernetes + Terraform (tag `v1.0`)

**Goal:** `make k3d-up` deploys the full stack to a local cluster; `terraform apply` stands up the same thing on AWS (cheapest viable footprint); `deploy.yml` rolls it out.

Deliverables:
1. Kustomize base: `backend` Deployment (+HPA on CPU and on `t2s_jobs_queue_depth` via custom metrics — optional stretch), `worker` Deployment, `frontend` Deployment, Postgres & Redis as StatefulSets for local overlay only, Ingress, ConfigMap/Secret, `ServiceMonitor` for kube-prometheus-stack, liveness/readiness probes, resource requests/limits, PodDisruptionBudget.
2. Local overlay targets **k3d** (k3s in Docker) — one command, no cloud bill, fully demoable on a laptop.
3. Terraform (`envs/dev`): VPC, EKS (or — cost-conscious alternative — a single EC2 running k3s; decision below), RDS Postgres, ElastiCache Redis, ECR, IAM roles (IRSA), S3 backend + DynamoDB state lock. Remote state and `terraform plan` in CI on PR.
4. Cloud overlay: external Postgres/Redis endpoints via Secrets (sourced from Terraform outputs → SSM/ExternalSecrets).
5. `deploy.yml`: on release → `kubectl apply -k` via OIDC-federated GitHub → AWS role (no long-lived keys).
6. kube-prometheus-stack via Helm with the Phase 2 dashboard auto-imported.
7. Cost note in README: what it costs per hour and a `make destroy` that actually tears down.

**Demo:** `k3d` locally always works; cloud demo is a recorded video + Terraform plan output, so the cluster doesn't have to be running during interviews.

### Stretch (post-v1.0, pick by interview target)
- pgvector instead of in-Redis embeddings; schema-link quality evals.
- Streaming SSE responses.
- Multi-tenant: per-database-connection schema graphs.
- OpenTelemetry traces → Tempo, linked from Grafana.
- Canary rollouts with Argo Rollouts.

---

## 4. Key design decisions and their tradeoffs (interview prep)

Each of these should become an ADR in `docs/decisions/` when implemented.

### D1. SQL safety: AST policy via sqlglot, plus DB-level defence in depth
- **Choice:** Parse with `sqlglot` (Postgres dialect). Accept only a single statement whose root is `Select` (or `With` whose final expression is a `Select` and whose CTEs are all selects). Walk the AST to reject: DML/DDL nodes, `Into`, `Lock` (`FOR UPDATE`), denylisted functions, `Command` nodes (sqlglot's fallback for unparseable statements). Verify every referenced table exists in the introspected schema. Inject `LIMIT n` if absent. Then execute on a **read-only role** inside a **read-only transaction** with `statement_timeout`.
- **Why not regex:** `SELECT … ; DROP …`, comments, string literals containing keywords, `COPY … TO PROGRAM`, function-based side effects — regex either over-blocks or under-blocks. AST is precise and explainable.
- **Tradeoffs:** sqlglot's Postgres dialect isn't 100% faithful — some valid Postgres it can't parse (reject, fail closed), some it parses differently from Postgres (hence DB-level role is the real guarantee). Denylist of functions is inherently incomplete → the read-only role + `statement_timeout` + row cap are the backstop. Cost: ~ms of parsing per query, negligible.
- **Interview line:** "The AST gate is for *UX and cheap rejection with a reason*; the Postgres role is the *security boundary*. Never trust the parser alone."

### D2. Schema retrieval: embeddings over table summaries + FK-hop expansion (no graph DB in v1)
- **Choice:** One embedding per table (name + comment + column names/types/comments). Top-k by cosine, then pull in tables 1 FK hop away so joins are possible. Render as compact DDL in the prompt.
- **Alternatives:** (a) dump whole schema — simplest, works for 11 tables, breaks at 200; (b) column-level embeddings — finer but noisier and more tokens; (c) LLM-driven schema linking ("which tables are relevant?") — extra LLM call, better on ambiguous questions; (d) Neo4j graph traversal — what you've done before; overkill for v1 but the FK-hop expansion is the same idea in-process.
- **Tradeoffs:** Table-level embeddings miss questions that hinge on a single column name ("customers with a `segment` of VIP"). Mitigation: hybrid BM25/keyword boost on column names (cheap to add). `k` and hop depth are tunable, and the eval set measures the impact — that's the story: *you can quantify retrieval changes.*
- **Why embeddings in Redis, not pgvector, in v2:** keeps the target DB untouched (we're read-only on it by design). pgvector on the app DB is a fine later move.

### D3. Cache design and invalidation
- **Result cache key** = hash(schema_version, llm model, prompt template version, normalised question). Any of those changing naturally produces a miss — no explicit invalidation storm needed. `schema_version` = hash of introspected DDL, recomputed on `/schema/refresh` (or on a cron job).
- **Staleness of underlying data:** The cache returns *rows*, and the target DB may change. Options: short TTL (default 5 min), cache only the generated SQL (always re-execute — cheap, always fresh, still skips the expensive LLM call), or both tiers. **Recommendation:** two tiers — SQL cache (long TTL, keyed as above) and result cache (short TTL, opt-in per request). This is the nuanced answer interviewers want.
- **Question normalisation:** lowercase, whitespace collapse, strip trailing punctuation. Semantic dedup via embedding similarity is a stretch goal with a false-positive risk worth naming.
- **Tradeoffs:** Redis adds an operational dependency; cache stampede on a hot key is possible (mitigate with a short lock or just accept it at this scale); cached errors must *not* be stored.

### D4. Sync vs async execution
- **Choice:** Both. Sync `POST /query` for the common path (LLM ~2–10 s is acceptable with a spinner); async `POST /query/async` + job polling for long-running or batch work, with the same pipeline code.
- **Why arq over Celery:** asyncio-native, Redis-only, ~200 lines of config vs Celery's weight; matches the FastAPI async stack. Celery wins if you need rate limits, chords, multiple brokers — say so.
- **Tradeoffs:** Polling vs SSE/WebSocket for job status — polling is simpler and works through any ingress; SSE is a stretch.

### D5. LLM provider abstraction
- **Choice:** A `Protocol` with `complete()` and `embed()` returning a `Usage(prompt_tokens, completion_tokens, latency_ms)`. Two adapters: OpenAI-compatible (covers OpenAI, Groq, Together, vLLM, LM Studio — most of the market) and Ollama native (for embeddings + local default).
- **Tradeoffs:** Lowest-common-denominator interface — no tool calling, no structured-output mode, no streaming in v1. Prompt-level JSON extraction instead (with a robust parser). Cost tracking needs a price table per model that will drift; keep it in config with a "unknown = 0" fallback and expose it as a metric anyway.
- **Why not LangChain/LlamaIndex:** dependency weight, abstraction churn, and it hides exactly the parts you want to demonstrate you understand.

### D6. Self-correction loop
- **Choice:** Exactly one repair attempt on DB error (syntax/unknown column). Feed the error text back.
- **Tradeoffs:** Doubles worst-case latency and cost; bounded at 1 keeps it predictable. Metric `stage=repair` shows how often it triggers — which is itself a prompt-quality signal.

### D7. Two databases, two roles
- **Choice:** App state (query logs, feedback, later eval results) lives in its own DB with a read/write role; the target DB is accessed only via a `readonly` role created by the seed script.
- **Tradeoffs:** Slightly more compose/Terraform config; in exchange, the blast radius of a guard bypass is zero, and the target DB can be *any* Postgres the user points at.

### D8. Observability choices
- **Metrics:** Prometheus pull model via `/metrics`; histograms with explicit buckets tuned for LLM latency (0.25 s → 30 s). Labels kept low-cardinality (no question text, no user id).
- **Logs:** structlog JSON, one event per pipeline stage, correlated by request id; the same id goes into the `QueryLog` row.
- **Tradeoffs:** Multiprocess gunicorn + prometheus_client needs the multiprocess dir dance — or run a single uvicorn worker per pod and scale horizontally (recommended; it's the k8s-native answer). Traces deferred to stretch.

### D9. Kubernetes and cloud footprint
- **Choice:** k3d for local/CI; for cloud, **EKS via Terraform** is the resume-relevant answer but costs ~$75/month for the control plane alone. Alternative: single EC2 + k3s (≈$10/month) with the *same manifests*. Recommendation: write the Terraform for EKS (it's what interviewers ask about), run it once for a recorded demo, and keep a `k3s-ec2` module as the "always-on cheap demo" option.
- **Tradeoffs:** Managed Postgres/Redis (RDS/ElastiCache) vs in-cluster StatefulSets: managed for cloud overlay, StatefulSets for local. Know why: backups, failover, and not running stateful workloads you don't have to.

### D10. Testing strategy
- **Unit:** guard/retrieve/render/prompt are pure functions → fast, exhaustive.
- **Integration:** testcontainers Postgres (seeded with the real `schema.sql`) + Redis, with `FakeLLM` so tests are deterministic and free.
- **Eval (not a test):** `scripts/eval.py` against a real LLM, reported as a number, not a pass/fail gate — because LLM output is non-deterministic and you don't want a flaky CI.
- **Tradeoffs:** testcontainers needs Docker in CI (fine on GitHub-hosted runners, a problem on some self-hosted ones); session-scoped containers to keep the suite under ~1 min.

---

## 5. Tooling decisions (small, but be consistent)

- Python: `uv` for deps/lockfile, `ruff` (lint+format), `mypy --strict` on `core/`, `pytest` + `pytest-asyncio` + `testcontainers`.
- Frontend: Vite + React 19 + TS, `vitest`, no state library, no UI kit beyond minimal CSS (or a single lightweight one) — it's deliberately thin.
- Containers: multi-stage Dockerfiles, non-root user, `HEALTHCHECK`, pinned base images.
- Docs: mermaid diagrams in markdown; ADR template (context / decision / consequences).

---

## 6. Open decisions for you to confirm before scaffolding

1. **Default local model:** `qwen2.5-coder:7b` via Ollama (good at SQL, ~5 GB) — or a smaller one if your machine is tight? Ollama in compose will be behind a profile so the stack works without a GPU using an external OpenAI-compatible key.
2. **Cloud target:** AWS (EKS + RDS + ElastiCache) as planned above, or do you want GCP/Azure to match a specific job target?
3. **EKS vs EC2+k3s** for the always-on demo (see D9). Recommendation: Terraform both, default to EC2+k3s for cost.
4. **Result cache tiers:** go with the two-tier (SQL cache + short-TTL result cache) recommendation in D3?
5. **Frontend tooling:** plain CSS vs. a minimal library (e.g. Tailwind). Recommendation: plain CSS + one code-highlight component.
6. **License / repo name:** MIT and `text2sql`? Git init on first scaffold.

Once you've answered (or said "go with your recommendations"), next step is scaffolding Phase 1.
