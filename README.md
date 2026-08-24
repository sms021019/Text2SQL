# Text2SQL

An open-source service that turns a natural-language question into a safe, read-only SQL query against a Postgres database, executes it, and returns results — built to showcase production infrastructure skills: containerised services, Kubernetes deployment, CI/CD, Redis caching, async job processing, Prometheus/Grafana observability, and Terraform-managed cloud infra. The LLM is pluggable (Ollama locally, any OpenAI-compatible API in the cloud) so the whole thing runs for free on a laptop with `docker compose up`.

<!-- CI badge lands here in Phase 3, once ci.yml exists. -->

## Architecture

```mermaid
flowchart TD
    Browser["Browser"] --> Frontend["nginx / frontend\n(static React app + /api proxy)"]
    Frontend --> API["FastAPI backend"]

    subgraph Pipeline["Text2SQL pipeline (backend/app/core)"]
        Retriever["Schema retriever\n(table embeddings + FK-hop expansion)"]
        Guard["sqlglot AST guard\n(SELECT-only, LIMIT injection)"]
        Executor["Read-only executor\n(readonly role, statement_timeout)"]
    end

    API --> Retriever
    Retriever --> Guard
    Guard --> Executor

    API --> AppDB[("app_db\nquery_log")]
    Executor --> TargetDB[("target_db\nNorthwindNext e-commerce")]

    API --> LLM["LLM provider\n(Ollama or OpenAI-compatible)"]
    Retriever -. embed .-> LLM
    API -. generate/repair .-> LLM
```

Two Postgres roles behind two logical databases: `app_db` (read/write) holds
the app's own state — right now just the `query_log` table — and `target_db`
(the seeded NorthwindNext e-commerce data) is reached only through a
`readonly` role. The LLM provider is out-of-process and swappable; nothing
in `app/core` talks to it directly except through the `LLMClient` protocol.

## Quickstart

```bash
git clone <this repo>
cd Text2SQL
cp .env.example .env
make up
```

Open http://localhost:5173, type a question, see the generated SQL and the
result table.

`make up` starts Postgres (seeded on first boot), the backend, and the
frontend — but *not* an LLM, so `/api/v1/query` will fail with an `llm:`
error until you point `LLM_PROVIDER` at something reachable. Two options:

* **Local model:** `make up-ollama` instead of `make up`. This also starts
  an `ollama` service and pulls `qwen2.5-coder:7b` (generation) plus
  `nomic-embed-text` (embeddings) — **roughly a 5 GB download** the first
  time, and CPU inference is slow without a GPU.
* **Hosted, OpenAI-compatible API:** edit `.env`, comment out the Ollama
  `LLM_*` lines, uncomment the OpenAI block, and fill in `LLM_API_KEY`. Then
  `make up` (no `ollama` profile needed).

## How a query flows

1. **Retrieve** — embed the question, cosine-rank every table against it
   (plus a keyword boost if the question mentions a table/column by name),
   take the top-k, then expand one FK hop so joins are reachable.
2. **Render** — turn the retrieved tables into compact DDL-ish text (names,
   types, comments, foreign keys) for the prompt.
3. **Generate** — one LLM call with the rendered schema and the question;
   the response is parsed for a SQL statement and a plain-English
   explanation.
4. **Guard** — parse the SQL with `sqlglot`, reject anything that isn't a
   read-only `SELECT`/`WITH ... SELECT` over known tables, inject a `LIMIT`
   if one is missing.
5. **Execute** — run it on a dedicated read-only connection
   (`SET TRANSACTION READ ONLY`, a `statement_timeout`, a row cap on the
   fetched result).
6. **Repair (conditional)** — if execution fails with a plausibly-fixable
   error (bad syntax, unknown column), the pipeline makes exactly one more
   LLM call with the error message and retries guard + execute once. A
   `GuardError` never triggers a repair — the pipeline does not ask the LLM
   to "fix" a rejected statement.

Every run — success or failure — is logged to `query_log` in `app_db` with
the question, SQL, timing per stage, token usage, and whether a repair fired.

## SQL safety

The `sqlglot` AST guard (`backend/app/core/sql/guard.py`) is a UX layer, not
the security boundary. It parses candidate SQL with the Postgres dialect and
walks the tree: only a single `SELECT`/`WITH ... SELECT` (or a set operation
of selects) is accepted, every CTE body must itself be a select, and the
tree is scanned for DML/DDL nodes, `SELECT ... INTO`, row locks
(`FOR UPDATE`/`FOR SHARE`), and calls to a denylist of dangerous functions
(sleeps/timing oracles, filesystem access, `dblink*`, advisory locks,
sequence mutation, session/backend state) matched by exact name and by
prefix families so unenumerated variants fail closed too. Every table
referenced must be a known table (from the retrieved schema) or a CTE alias
visible in its own scope — an unqualified name can bind to a CTE, a
schema-qualified one can't. On acceptance, a `LIMIT` is injected or clamped
to `max_rows` on the outermost query only.

The real guarantee is one layer down: generated SQL only ever runs through
`execute_readonly` (`backend/app/core/sql/executor.py`), which opens a
connection on the Postgres `readonly` role, forces
`SET TRANSACTION READ ONLY` and a per-statement `statement_timeout` itself
(not relying on the role's own server-side defaults), and always rolls the
transaction back — it never commits. `sqlglot`'s Postgres dialect isn't
perfectly faithful to real Postgres and a denylist is inherently incomplete,
so treat any guard bypass as a UX bug, not a breach: the read-only role,
timeout, and row cap are what actually make a malicious or malformed
statement harmless.

## Switching LLM providers

Set three env vars (see `.env.example`):

```bash
LLM_PROVIDER=ollama            # or "openai"
LLM_BASE_URL=http://ollama:11434
LLM_MODEL=qwen2.5-coder:7b
EMBED_MODEL=nomic-embed-text
LLM_API_KEY=unused
```

`LLM_PROVIDER=openai` selects `OpenAICompatClient`, which works with the
OpenAI API itself and anything that speaks the same wire format (Groq,
Together, vLLM, LM Studio, ...) — just point `LLM_BASE_URL` at it and set
`LLM_MODEL`/`EMBED_MODEL`/`LLM_API_KEY` accordingly. Both adapters implement
the same `LLMClient` protocol (`complete()`, `embed()`), so nothing else in
the pipeline changes.

## Running tests

```bash
make test      # cd backend && uv run pytest -q — needs Docker (testcontainers spins up real Postgres)
make lint      # ruff check, ruff format --check, mypy app — all in backend/
make fe-test   # cd frontend && npm test
```

`make test` runs both unit tests (guard, retrieve, render — pure functions,
no containers) and integration tests (the full pipeline against a real,
freshly-seeded Postgres with a `FakeLLM`, via `testcontainers`).

## Evaluation

`scripts/eval.py` runs every question in `seed/questions.yaml` through a
live `POST /api/v1/query` and reports two numbers: the percentage of
questions that came back executable (no pipeline error) and, among those
with a known `expect_rows`, the percentage whose `row_count` matched.

**These numbers were not measured during the Phase 1 build** — no LLM was
reachable on the build machine (the local Ollama model pull did not
complete, and no hosted API key was configured), so `eval.py` was
smoke-tested with `--dry-run` only. Once a provider is reachable, produce
real numbers with:

```bash
make up-ollama          # or configure the OpenAI block in .env, then `make up`
cd backend && python -m uv run python ../scripts/eval.py
```

This prints a per-question table (executable / row-match / latency /
tokens) plus:

```
executable: X/30 (P%)
row_match: Y/M (Q%)
```

and writes the full per-question results to `eval-results.json` (gitignored).
Add `--limit N` to try a handful of questions first, `--dry-run` to just
validate `questions.yaml` and print the question list without calling the
API, and `--api-url`/`--questions`/`--out` to point at something other than
the defaults.

## Roadmap

This is Phase 1 of a four-phase plan — see [`PLAN.md`](PLAN.md) for the full
detail and the design-decision writeups (§4) that the ADRs in
[`docs/decisions/`](docs/decisions/) are drawn from:

* **Phase 1 (this tag, `v0.1`)** — core NL→SQL API, local `docker compose up`.
* **Phase 2 (`v0.2`)** — Redis result/schema caching, an async job queue,
  Prometheus + Grafana observability.
* **Phase 3 (`v0.3`)** — CI on every PR, multi-arch image releases to GHCR.
* **Phase 4 (`v1.0`)** — Kubernetes (k3d locally, Terraform-provisioned EKS
  in the cloud).

## Project layout

```
Text2SQL/
├── backend/     # FastAPI app: core pipeline, LLM clients, API routes, tests
├── frontend/    # Vite + React + TS single-page UI
├── seed/        # NorthwindNext schema, generator, seed init container, eval questions
├── scripts/     # eval.py (and, from Phase 2, load_test.js)
├── docs/        # architecture.md, demo.md, ADRs
├── docker-compose.yml
├── .env.example
├── Makefile
└── PLAN.md
```

## License

MIT — see [`LICENSE`](LICENSE).
