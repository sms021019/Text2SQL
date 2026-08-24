# Demo script (~2 minutes)

Assumes `cp .env.example .env` has been run once. This script uses
`make up-ollama` so it's fully self-contained; if you've already configured
an OpenAI-compatible provider in `.env`, use `make up` instead and skip the
Ollama-specific notes.

## 0. Start the stack (~30s narration while it comes up)

```bash
make up-ollama
```

This builds and starts `postgres` (seeded with the NorthwindNext
e-commerce schema on first boot), `backend`, `frontend`, `ollama`, and a
one-shot `ollama-pull` container that pulls `qwen2.5-coder:7b` and
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
python -c "
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
for `scripts/eval.py` and, from Phase 2, the Grafana dashboards.

`Ctrl+C` to stop tailing, then `make down` to tear the stack down.
