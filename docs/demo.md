# Demo script (~3 minutes)

Sections 0–5 are the Phase 1 core (~2 minutes); sections 6–9 are the
Phase 2 additions — cache, background jobs, metrics, dashboard, load test.

Assumes `cp .env.example .env` has been run once. This script uses
`make up-ollama` so it's fully self-contained; if you've already configured
an OpenAI-compatible provider in `.env`, use `make up` instead and skip the
Ollama-specific notes.

## 0. Start the stack (~30s narration while it comes up)

```bash
make up-ollama
```

This builds and starts `postgres` (seeded with the NorthwindNext
e-commerce schema on first boot), `redis`, `backend`, the arq `worker`,
`frontend`, `prometheus`, `grafana`, `ollama`, and a one-shot
`ollama-pull` container that pulls `qwen2.5-coder:7b` and
`nomic-embed-text` (~5 GB the first time — for a live demo, pre-pull this
ahead of time so it isn't part of the two minutes).

Wait for the backend to report ready:

```bash
curl http://localhost:8000/readyz
# {"status":"ok"}
```

## 1. Ask a question in the browser

Open **http://localhost:5173**. Type:

> top 5 products by revenue

Click submit. You should see:

* the generated SQL in the read-only SQL panel (a `SELECT` joining
  `order_items` and `products`, grouped and ordered by revenue, `LIMIT 5`);
* a 5-row result table;
* the schema sidebar showing which tables were retrieved for this question.

## 2. The same question via curl, showing the full response shape

```bash
curl -s http://localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "top 5 products by revenue"}' | python -m json.tool
```

Expected shape (values will vary):

```json
{
  "sql": "SELECT p.id, p.name, SUM(oi.unit_price * oi.quantity) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.id, p.name ORDER BY revenue DESC LIMIT 5",
  "explanation": "Joins order line items to products and sums revenue per product.",
  "columns": ["id", "name", "revenue"],
  "rows": [[42, "Wireless Mouse", 18342.50], "... 4 more rows ..."],
  "row_count": 5,
  "truncated": false,
  "tables": ["order_items", "products"],
  "repaired": false,
  "timings": [{"stage": "retrieve", "ms": 12.3}, "..."],
  "usage": {"prompt_tokens": 612, "completion_tokens": 84, "latency_ms": 1904.2},
  "error": null,
  "request_id": "..."
}
```

## 3. Show the guard blocking a write — through the API

The API only accepts natural language; there's no SQL input to hand-craft
a `DELETE` into. Ask it to do a write anyway and watch two independent
layers refuse it:

```bash
curl -s http://localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "delete all orders"}' | python -m json.tool
```

Two things can happen, both of which demonstrate the guard, and worth
narrating either way:

* **The LLM actually emits a `DELETE`** — most instruction-tuned models
  will, since the question is a literal imperative. The AST guard rejects
  it before it ever reaches Postgres, and the response comes back with
  `"error": "guard:not_select: expected a SELECT, found DELETE"`,
  `"sql"` still showing what the model produced, and `"row_count": 0`.
* **The LLM refuses or produces something else** — e.g. it emits a
  `SELECT * FROM orders` instead ("here are the orders you asked about")
  or plain refusal text the parser can't turn into SQL
  (`"error": "parse: ..."`). Either way, nothing gets deleted: the guard
  is the reason a `DELETE` *can't* reach the database, not a promise
  about what any given model will try.

## 4. Show the guard directly (unit-test style)

To see the guard's actual decision, independent of what any particular LLM
does, call it directly:

```bash
cd backend
python -m uv run python -c "
from app.core.sql.guard import guard_sql, GuardError
try:
    guard_sql('DELETE FROM orders', {'orders'}, max_rows=500)
except GuardError as e:
    print(f'blocked: {e.reason}: {e.detail}')
"
```

Expected output:

```
blocked: not_select: expected a SELECT, found DELETE
```

This is the same code path `Text2SQLPipeline.run()` hits when the LLM's
SQL fails guarding — a `GuardError` is always terminal there, deliberately:
the pipeline never asks the LLM to "fix" a `DELETE` (see
`backend/app/core/pipeline.py`'s module docstring).

## 5. Structured logs

```bash
make logs
```

Each pipeline stage for the requests above logs a JSON line, e.g.:

```json
{"event": "stage", "stage": "guard", "ms": 1.8, "request_id": "...", "level": "info", "timestamp": "..."}
```

correlated by `request_id`, the same id returned in the API response and
stored on the corresponding `query_log` row in `app_db` — the data source
for `scripts/eval.py`.

`Ctrl+C` to stop tailing.

## 6. Ask the same question again — the cache

Back in the browser, submit **exactly the same question** a second time.
The answer comes back without an LLM round trip and the badge under the
SQL panel now reads `cache: sql hit` instead of `cache: miss`.

Same thing via curl:

```bash
curl -s http://localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "top 5 products by revenue"}' \
  | python -c 'import json,sys; d=json.load(sys.stdin); print(d["cache_status"], d["usage"])'
# sql_hit {'prompt_tokens': 0, 'completion_tokens': 0, 'latency_ms': 0.0}
```

Narration: the *SQL* is cached for a day, the *rows* are not — the cached
statement is re-guarded and re-executed every time, so the data is fresh.
The short-TTL result cache that skips execution too is opt-in per request
(`"use_result_cache": true`), and `"use_cache": false` bypasses both
(`cache_status: bypass`). See [ADR 0003](decisions/0003-cache-design.md).

## 7. Run it in the background — the job queue

Tick **Run in background** in the UI and submit; the app enqueues the
question and polls until the answer arrives. Or:

```bash
JOB=$(curl -s -X POST http://localhost:8000/api/v1/query/async \
  -H 'Content-Type: application/json' \
  -d '{"question": "how many orders are there for each status?"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -s http://localhost:8000/api/v1/jobs/$JOB | python -m json.tool
# {"job_id": "...", "status": "queued", "result": null, "error": null}
# ... poll again a few seconds later ...
# {"job_id": "...", "status": "complete", "result": { ...the same QueryResponse... }}
```

The job runs in the separate `worker` container, on the same pipeline and
the same caches as the synchronous route — same request body in, same
response shape out ([ADR 0004](decisions/0004-async-jobs.md)).

## 8. The raw metrics

```bash
curl -s http://localhost:8000/metrics/ | grep t2s_cache_requests_total
```

```
t2s_cache_requests_total{cache="schema",outcome="hit"} 1.0
t2s_cache_requests_total{cache="sql",outcome="miss"} 1.0
t2s_cache_requests_total{cache="sql",outcome="hit"} 1.0
t2s_cache_requests_total{cache="result",outcome="bypass"} 2.0
```

(The trailing slash matters: `/metrics` is a mounted sub-app, so without it
Starlette answers a 307 — `curl -sL` works too.)

Every `t2s_*` metric comes from the pipeline's observer hook, not from
framework middleware: `app/core` reports events, the app layer decides they
become Prometheus counters ([ADR 0005](decisions/0005-observability.md)).

## 9. The dashboard, under load

Open **http://localhost:3000** (anonymous access, no login) →
**Dashboards → Text2SQL overview**. Then, in another terminal:

```bash
make load-test
# Docker Desktop (macOS/Windows):
# API_URL=http://host.docker.internal:8000 make load-test
```

5 virtual users for 60 seconds, walking ten real questions from
`seed/questions.yaml` — offset per VU, so questions repeat during the run
and the SQL cache starts hitting — with `use_cache` flipped every other
iteration, so half the traffic bypasses the cache and the hit-rate panel
reflects only cache-enabled requests. Prometheus scrapes every 5 s, so
within a few seconds the panels
move: LLM p50/p95 by stage, SQL success rate, cache hit rate per tier,
tokens per minute and cost, queue depth, HTTP p95, guard rejections.

Then `make down` to tear the stack down.
