# 0005. Observability: an observer hook in the core, Prometheus at the edge

## Status

Accepted — implemented in `backend/app/core/observer.py`,
`backend/app/observability/**`, and the `prometheus`/`grafana` services with
their provisioning under `deploy/`.

## Context

The pipeline is where every interesting number lives: how long the LLM
took, how many tokens it burned, whether the guard rejected the statement,
whether the cache hit, whether the query executed. But `app/core/**` is
deliberately framework-free — it may not import fastapi, redis, or
`app.api` (see `app/core/errors.py`), and adding `prometheus_client` to
that list would make the core depend on a specific metrics backend forever.

The Phase 2 demo also needs the numbers to be *visible*, not merely
exposed: a dashboard someone can look at while a load test runs.

## Decision

1. **`PipelineObserver`, a structural `Protocol` in `app/core/observer.py`**
   with one method per event: `on_stage`, `on_llm`, `on_guard_reject`,
   `on_execution`, `on_cache`, `on_pipeline_done`. The core reports what
   happened; it knows nothing about what the report becomes. The module
   imports only `typing` and `app.llm.base.Usage` (which
   `app/core/pipeline.py` already depended on), so the core's dependency
   surface does not widen. `NullObserver` is the default, making
   observability opt-in and free when unused.

2. **`MetricsObserver` lives in the app layer** (`app/observability/`),
   where importing `prometheus_client` is fine, and turns those events into
   `t2s_*` collectors. `CompositeObserver` fans events out to several
   observers, so an injected observer (a test's recorder) and the metrics
   observer can both receive every event instead of the caller choosing one.

3. **One `Metrics` per app, owning its own `CollectorRegistry`.** Built in
   `create_app()`, never at import time: the process-global default registry
   would make every second app in a test process collide on duplicate
   collector names. `/metrics` is exposed against that same registry by
   `Instrumentator(registry=...).instrument(app).expose(app)` -- a plain
   route rather than a mounted sub-app, so a slash-less `GET /metrics`
   answers directly instead of 307ing -- and `METRICS_ENABLED=false` skips
   instrumentation and the endpoint entirely.

4. **The metric set** (names fixed, all `t2s_`-prefixed):
   `t2s_llm_request_duration_seconds{provider,model,stage}` (buckets
   0.25 s → 60 s, wide because completions take seconds),
   `t2s_llm_tokens_total{provider,model,direction}`,
   `t2s_llm_cost_usd_total{provider,model}`,
   `t2s_sql_guard_rejections_total{reason}`,
   `t2s_sql_execution_total{outcome}`,
   `t2s_sql_execution_duration_seconds`,
   `t2s_cache_requests_total{cache,outcome}`,
   `t2s_pipeline_duration_seconds{ok}`, `t2s_jobs_queue_depth`, and
   `t2s_schema_version_info{version}`. HTTP-level metrics
   (`http_requests_total`, `http_request_duration_seconds{handler,method}`,
   `http_request_duration_highr_seconds`) come from
   `prometheus-fastapi-instrumentator`, with `/metrics`, `/healthz` and
   `/readyz` excluded so health checks and scrapes don't dominate the
   latency histograms.

5. **Labels are low-cardinality by rule, not by habit.** The only permitted
   label values are the closed sets above — `provider`, `model`, `stage`,
   `direction`, `reason`, `outcome`, `cache`, `ok`, plus the single
   `version` on the schema gauge. Never the question text, the request id,
   the SQL, or a user id: those belong in the structured logs, where
   cardinality is free. `t2s_schema_version_info` is `.clear()`-ed before
   each set so a refresh leaves exactly one version reading 1.

6. **Cost is estimated from a price table in config.**
   `LLM_PRICES_USD_PER_1K` maps model → (USD per 1k prompt tokens, USD per
   1k completion tokens); a model absent from it costs **0**, which is the
   correct answer for every local Ollama model and a visible, harmless
   under-report for an unlisted hosted one.

7. **Logs stay the high-cardinality half.** structlog JSON, one event per
   pipeline stage, correlated by `request_id` — the same id returned in the
   response and stored on the `query_log` row.

8. **Prometheus and Grafana are always-on compose services**, not a profile:
   the dashboard *is* the Phase 2 deliverable. Prometheus scrapes
   `backend:8000/metrics` every 5 s (short, so a 60 s `make load-test` run
   visibly moves the panels). Grafana provisions its datasource and the
   `Text2SQL overview` dashboard from files under `deploy/grafana/`, with
   anonymous Viewer access so the demo needs no login.

## Alternatives considered

- **Call `prometheus_client` directly from the pipeline.** Fewer moving
  parts, but it welds the core to one metrics backend and makes the
  pipeline untestable without a registry. The `Protocol` costs one file.
- **OpenTelemetry traces (and OTLP metrics).** The better answer for
  cross-service causality, and the natural Phase 4 addition; for a
  single-service pipeline the span tree would mostly restate the stage
  timings already in the logs, at the cost of a collector in the stack.
- **Push-based metrics (Pushgateway/StatsD).** Would let the arq worker
  report too, without an HTTP endpoint. Rejected for now: pull is the
  Kubernetes-native model and the Pushgateway's stale-metric semantics are
  a known footgun. Scraping the worker properly means giving it a small
  metrics endpoint — a follow-up if job-level metrics start to matter.
- **Deriving dashboards from `query_log` in Postgres.** Already possible for
  after-the-fact analysis and used by `scripts/eval.py`, but it is a
  reporting database, not a time-series one; percentiles and rates over it
  are awkward and unbounded in cost.

## Consequences

- Anything the pipeline does not report to the observer cannot be graphed.
  Adding a metric means adding an event, which is a deliberate speed bump
  against instrumenting by reflex.
- `MetricsObserver.on_stage` is intentionally a no-op: per-stage timing is
  already covered by the LLM and SQL histograms and by the structured logs,
  and a histogram per stage label would add series for little gain.
- **Multiple workers per process would break the registry.** `prometheus_client`
  needs its multiprocess-directory dance under gunicorn; the deployment
  answer here is one uvicorn worker per container and horizontal scaling —
  which is what Phase 4's Kubernetes manifests will do anyway.
- **`t2s_llm_request_duration_seconds` has no `stage="embed"` series, even
  though an embedding call happens on every full-path request.**
  `SchemaRetriever._score_tables` calls `LLMClient.embed()` to embed the
  question (`app/core/schema/retrieve.py`), but `embed()` returns bare
  vectors and no `Usage`, so there is nothing for the pipeline to hand to
  `observer.on_llm(...)` — only `generate` and `repair` reach it. The
  embedding latency is therefore folded into
  `t2s_pipeline_duration_seconds` and visible per-stage only in the logs.
  Instrumenting it means widening `LLMClient.embed` to return a `Usage`
  (both adapters, plus `FakeLLM`), which is deferred: the plan's metric set
  lists `stage=embed`, but the interface change is a larger edit than the
  Phase 2 brief covers.
- The arq worker's metrics are recorded into a registry nothing scrapes
  (see ADR 0004). Job-level numbers come from its logs until it gets an
  endpoint of its own.
- Cost figures are estimates and drift with vendor pricing; the table is
  config, not code, so correcting it is an env var away — but nothing
  validates it against a bill.
- Counters reset when the backend process restarts, so cumulative panels
  (notably the cost stat) show "since last restart", not all-time. Rates
  are unaffected.
