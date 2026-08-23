# Phase 1 — Core NL→SQL API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `git clone && make up` → browser at `localhost:5173` → ask a question about the seeded e-commerce DB → see generated SQL + rows, with SELECT-only enforcement via sqlglot and a full test suite.

**Architecture:** FastAPI backend with a framework-free `app/core` pipeline (introspect → retrieve → prompt → generate → guard → execute → one repair). LLM behind a `Protocol` with Ollama + OpenAI-compatible adapters. Two Postgres databases in one container (`app` read/write, `target` via a `readonly` role). Thin Vite/React frontend. Tests: pytest unit tests for pure modules, testcontainers Postgres for the pipeline with a `FakeLLM`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async, asyncpg), sqlglot, pydantic-settings, jinja2, httpx, structlog, Alembic, pytest + pytest-asyncio + testcontainers, uv, ruff, mypy; React 19 + TypeScript + Vite; Docker Compose.

**Spec:** `PLAN.md` (sections 1, 2, 3/Phase 1, 4)

## Global Constraints

- Python `>=3.12,<3.13` in `pyproject.toml`; images use `python:3.12-slim`.
- `app/core/**` must not import `fastapi`, `redis`, or `app.api`.
- SELECT-only enforcement is AST-based via `sqlglot` — never regex on the SQL string.
- Target DB is accessed only through the `readonly` Postgres role in a read-only transaction with `statement_timeout`.
- Every test must pass with no network and no real LLM (`FakeLLM`).
- Seed generator is deterministic (`random.seed(42)`, `Faker.seed(42)`).
- Commit after every task; conventional-commit messages; co-author trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Windows host: use Git Bash (`Bash` tool) for all commands; paths with forward slashes.

---

## File map (created in this phase)

| Path | Responsibility |
|---|---|
| `backend/pyproject.toml` | deps, ruff/mypy/pytest config |
| `backend/app/config.py` | `Settings` (pydantic-settings) |
| `backend/app/llm/base.py` | `LLMClient` Protocol, `Completion`, `Usage` |
| `backend/app/llm/openai_compat.py` | OpenAI-compatible adapter |
| `backend/app/llm/ollama.py` | Ollama adapter |
| `backend/app/llm/factory.py` | `build_llm(settings)` |
| `backend/app/core/errors.py` | domain exceptions |
| `backend/app/core/schema/models.py` | `Column`, `Table`, `SchemaGraph` |
| `backend/app/core/schema/introspect.py` | SQLAlchemy Inspector → `SchemaGraph` |
| `backend/app/core/schema/render.py` | `SchemaGraph` subset → DDL text |
| `backend/app/core/schema/retrieve.py` | embeddings + FK-hop retrieval |
| `backend/app/core/sql/guard.py` | sqlglot policy |
| `backend/app/core/sql/executor.py` | read-only execution |
| `backend/app/core/prompting/templates/generate.j2`, `repair.j2` | prompts |
| `backend/app/core/prompting/builder.py` | render prompts, parse LLM output |
| `backend/app/core/pipeline.py` | orchestration |
| `backend/app/db/session.py` | two async engines |
| `backend/app/db/models.py` | `QueryLog` ORM |
| `backend/alembic/*` | migrations for app DB |
| `backend/app/observability/logging.py` | structlog config |
| `backend/app/api/deps.py`, `api/v1/*.py`, `app/main.py` | HTTP layer |
| `backend/tests/**` | tests |
| `seed/schema.sql`, `seed/generate.py`, `seed/questions.yaml`, `seed/init.sh` | dataset |
| `frontend/**` | UI |
| `docker-compose.yml`, `Makefile`, `.env.example`, `README.md`, `docs/demo.md` | glue |
| `scripts/eval.py` | accuracy eval |

---

### Task 1: Repo bootstrap + backend project skeleton

**Files:**
- Create: `.gitignore`, `LICENSE`, `Makefile`, `.env.example`, `backend/pyproject.toml`, `backend/app/__init__.py`, `backend/app/config.py`, `backend/tests/__init__.py`, `backend/tests/unit/test_config.py`

**Interfaces:**
- Produces: `app.config.Settings` with fields below and `get_settings()` (lru_cache).

- [ ] **Step 1: git init + install uv**

```bash
cd /c/Users/sms02/minseokseo/Text2SQL && git init -b main
pip install uv
```

- [ ] **Step 2: Write `.gitignore`, `LICENSE` (MIT, copyright "Minseok Seo"), `Makefile`**

`.gitignore`:
```
__pycache__/
*.pyc
.venv/
.env
node_modules/
dist/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
.coverage
htmlcov/
```

`Makefile`:
```make
.PHONY: up down logs test lint seed eval
up:        ; docker compose up --build -d
down:      ; docker compose down -v
logs:      ; docker compose logs -f backend
test:      ; cd backend && uv run pytest -q
lint:      ; cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app/core
seed:      ; docker compose run --rm seed
eval:      ; cd backend && uv run python ../scripts/eval.py
```

- [ ] **Step 3: Write `backend/pyproject.toml`**

```toml
[project]
name = "text2sql-backend"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0",
  "asyncpg>=0.29",
  "psycopg[binary]>=3.2",
  "alembic>=1.13",
  "pydantic>=2.8",
  "pydantic-settings>=2.4",
  "sqlglot>=25",
  "jinja2>=3.1",
  "httpx>=0.27",
  "structlog>=24",
  "numpy>=2.0",
]

[dependency-groups]
dev = [
  "pytest>=8", "pytest-asyncio>=0.24", "testcontainers[postgres]>=4.8",
  "ruff>=0.6", "mypy>=1.11", "httpx>=0.27",
]

[tool.ruff]
line-length = 100
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "ASYNC"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["integration: needs docker"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

- [ ] **Step 4: Write failing test `backend/tests/unit/test_config.py`**

```python
from app.config import Settings


def test_defaults_point_at_local_stack(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.target_db_url.startswith("postgresql+asyncpg://readonly")
    assert s.max_rows == 500
```

- [ ] **Step 5: Run it, expect ImportError**

`cd backend && uv python install 3.12 && uv sync && uv run pytest tests/unit/test_config.py -v`

- [ ] **Step 6: Write `backend/app/config.py`**

```python
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_db_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    target_db_url: str = "postgresql+asyncpg://readonly:readonly@localhost:5432/target"

    llm_provider: Literal["ollama", "openai"] = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = "unused"
    llm_model: str = "qwen2.5-coder:7b"
    embed_model: str = "nomic-embed-text"
    llm_timeout_s: float = 60.0

    retrieve_top_k: int = 4
    max_rows: int = 500
    statement_timeout_ms: int = 5000
    prompt_version: str = "v1"
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 7: Run test → PASS. Commit.**

```bash
git add -A && git commit -m "chore: bootstrap repo and backend skeleton"
```

---

### Task 2: Seed dataset (schema + generator + questions)

**Files:**
- Create: `seed/schema.sql`, `seed/roles.sql`, `seed/generate.py`, `seed/init.sh`, `seed/Dockerfile`, `seed/questions.yaml`, `backend/tests/integration/test_seed.py`

**Interfaces:**
- Produces: target DB with the 11 tables from PLAN.md §2 and a `readonly` role; `seed/generate.py` writes `seed/data/*.csv` deterministically; `questions.yaml` entries `{id, question, sql, expect_rows?}`.

- [ ] **Step 1: Write `seed/schema.sql`** — all 11 tables with FKs, `CHECK`/enum constraints, and `COMMENT ON` for every table and key columns. Status enum via `CREATE TYPE order_status AS ENUM ('pending','paid','shipped','cancelled','refunded')`. `order_items.unit_price NUMERIC(10,2)` commented "price at time of order; do not use products.unit_price for revenue". `products.discontinued_at TIMESTAMPTZ NULL` commented "NULL = active". Indexes on all FKs and `orders.order_date`.

- [ ] **Step 2: Write `seed/roles.sql`**

```sql
CREATE ROLE readonly LOGIN PASSWORD 'readonly';
GRANT CONNECT ON DATABASE target TO readonly;
\c target
GRANT USAGE ON SCHEMA public TO readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly;
ALTER ROLE readonly SET default_transaction_read_only = on;
ALTER ROLE readonly SET statement_timeout = '5s';
```

- [ ] **Step 3: Write `seed/generate.py`** — stdlib + `faker`; `random.seed(42)`; `Faker.seed(42)`; row counts per PLAN.md §2; writes CSVs to `seed/data/`; order dates spread over the trailing 24 months relative to a **fixed** anchor `2026-08-01` (not `today`, for reproducibility); order_items.unit_price = product price ± small jitter; 10% of orders cancelled, 3% refunded; payments only for paid/shipped/refunded; shipments only for shipped.

- [ ] **Step 4: Write `seed/init.sh`** (runs inside the Postgres image's `/docker-entrypoint-initdb.d/`)

```bash
#!/usr/bin/env bash
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" <<-SQL
  CREATE DATABASE app;
  CREATE ROLE app LOGIN PASSWORD 'app';
  GRANT ALL PRIVILEGES ON DATABASE app TO app;
  CREATE DATABASE target;
SQL
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d target -f /seed/schema.sql
for t in customers addresses categories suppliers products inventory orders order_items payments shipments reviews; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d target -c "\copy $t FROM '/seed/data/$t.csv' CSV HEADER"
done
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -f /seed/roles.sql
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d app -c "GRANT ALL ON SCHEMA public TO app;"
```

CSVs are generated at image build time: `seed/Dockerfile` = `FROM python:3.12-slim; pip install faker; COPY . /seed; RUN python /seed/generate.py` producing `/seed/data`. The compose `postgres` service mounts from this image via a named volume populated by a one-shot `seed` service (or simpler: commit generated CSVs? **No** — keep repo small; generate at build).

- [ ] **Step 5: Write `seed/questions.yaml`** — 30 entries. Mix: 8 single-table, 10 joins, 8 aggregates/top-N, 4 date-range. Each has `sql` (hand-written, verified in Step 7) and `expect_rows` where deterministic.

- [ ] **Step 6: Write integration test `backend/tests/integration/test_seed.py`** using a session fixture in `tests/conftest.py`:

```python
# tests/conftest.py
import pathlib, subprocess, pytest
from testcontainers.postgres import PostgresContainer

SEED = pathlib.Path(__file__).resolve().parents[2] / "seed"

@pytest.fixture(scope="session")
def pg():
    with PostgresContainer("postgres:16-alpine", username="postgres", password="postgres", dbname="postgres") as c:
        yield c

@pytest.fixture(scope="session")
def target_db_url(pg):  # sync psycopg url for seeding
    import psycopg
    admin = pg.get_connection_url(driver=None)
    subprocess.run(["python", str(SEED / "generate.py")], check=True)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE DATABASE target")
        conn.execute("CREATE DATABASE app"); conn.execute("CREATE ROLE app LOGIN PASSWORD 'app'")
        conn.execute("GRANT ALL PRIVILEGES ON DATABASE app TO app")
    t = admin.replace("/postgres", "/target")
    with psycopg.connect(t, autocommit=True) as conn:
        conn.execute((SEED / "schema.sql").read_text())
        with conn.cursor() as cur:
            for tbl in ["customers","addresses","categories","suppliers","products","inventory","orders","order_items","payments","shipments","reviews"]:
                with open(SEED / "data" / f"{tbl}.csv") as f, cur.copy(f"COPY {tbl} FROM STDIN CSV HEADER") as cp:
                    cp.write(f.read())
        conn.execute((SEED / "roles.sql").read_text().replace("\\c target", ""))
    return t

@pytest.fixture(scope="session")
def readonly_async_url(target_db_url):
    host_port = target_db_url.split("@")[1]
    return f"postgresql+asyncpg://readonly:readonly@{host_port}"
```

```python
# tests/integration/test_seed.py
import psycopg, pytest
pytestmark = pytest.mark.integration

def test_row_counts(target_db_url):
    with psycopg.connect(target_db_url) as c:
        assert c.execute("select count(*) from orders").fetchone()[0] == 20000
        assert c.execute("select count(*) from order_items").fetchone()[0] >= 50000

def test_readonly_role_cannot_write(target_db_url):
    ro = target_db_url.replace("postgres:postgres", "readonly:readonly")
    with psycopg.connect(ro) as c, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        c.execute("delete from reviews")
```

- [ ] **Step 7: Run `uv run pytest tests/integration/test_seed.py -v`** → PASS. Then run every `sql` in `questions.yaml` via a small throwaway loop to confirm they execute and fill `expect_rows`. Commit.

```bash
git add -A && git commit -m "feat(seed): e-commerce schema, deterministic generator, eval questions"
```

---

### Task 3: LLM client abstraction (Protocol + Ollama + OpenAI-compatible + FakeLLM)

**Files:**
- Create: `backend/app/llm/base.py`, `ollama.py`, `openai_compat.py`, `factory.py`, `backend/tests/fakes/llm.py`, `backend/tests/unit/test_llm_clients.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class Usage: prompt_tokens: int; completion_tokens: int; latency_ms: float
@dataclass(frozen=True)
class Completion: text: str; usage: Usage; model: str
class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
def build_llm(settings: Settings) -> LLMClient
```
- `FakeLLM(responses: list[str] | dict[str, str], dim=8)` — returns responses in order (or by substring match on user prompt); `embed` returns deterministic vectors from `hashlib` of the text so similarity is stable.

- [ ] **Step 1: Write tests** using `httpx.MockTransport` — for Ollama: `POST /api/chat` returns `{"message":{"content":"SELECT 1"},"prompt_eval_count":10,"eval_count":3}`; `POST /api/embed` returns `{"embeddings":[[0.1,0.2]]}`. For OpenAI: `POST /v1/chat/completions` and `/v1/embeddings` standard shapes, `Authorization: Bearer` header asserted. Test that `build_llm` returns the right class for each provider. Test `FakeLLM` ordering and embed determinism.
- [ ] **Step 2: Run → ImportError.**
- [ ] **Step 3: Implement** — each adapter takes `(base_url, model, embed_model, api_key, timeout, transport=None)`; measure latency with `time.perf_counter()`; raise `LLMError` (defined in `app/core/errors.py`, created here) on non-2xx.
- [ ] **Step 4: Tests pass. Commit** `feat(llm): provider-agnostic client with ollama and openai adapters`.

---

### Task 4: Schema models + introspection + render

**Files:**
- Create: `backend/app/core/schema/models.py`, `introspect.py`, `render.py`, `backend/tests/unit/test_render.py`, `backend/tests/integration/test_introspect.py`

**Interfaces:**
- Produces:
```python
class Column(BaseModel): name: str; type: str; nullable: bool; comment: str | None; is_pk: bool
class ForeignKey(BaseModel): column: str; ref_table: str; ref_column: str
class Table(BaseModel): name: str; comment: str | None; columns: list[Column]; foreign_keys: list[ForeignKey]
    def summary(self) -> str   # "orders: Customer orders. columns: id, customer_id, status, ..."
class SchemaGraph(BaseModel):
    tables: dict[str, Table]
    version: str                # sha256 of canonical DDL render
    def neighbors(self, table: str) -> set[str]   # FK out + FK in
async def introspect(engine: AsyncEngine) -> SchemaGraph
def render_ddl(graph: SchemaGraph, tables: Iterable[str]) -> str
```

- [ ] **Step 1: Unit test `render_ddl`** on a hand-built 2-table graph: output contains `CREATE TABLE orders (`, column comments as `-- ...` trailing, `FOREIGN KEY (customer_id) REFERENCES customers(id)`; `version` stable across two constructions and changes when a column is added.
- [ ] **Step 2: Integration test `introspect`** against `readonly_async_url`: 11 tables, `orders` has FK to `customers`, `order_items.unit_price.comment` contains "price at time of order", `neighbors("orders")` ⊇ `{customers, order_items, payments, shipments, addresses}`.
- [ ] **Step 3: Run → fail.**
- [ ] **Step 4: Implement.** `introspect` uses `conn.run_sync(lambda c: inspect(c))` then `get_table_names/get_columns/get_pk_constraint/get_foreign_keys/get_table_comment`. Version = `hashlib.sha256(render_ddl(graph, sorted(tables)).encode()).hexdigest()[:12]`.
- [ ] **Step 5: Pass. Commit** `feat(schema): introspection, graph model, DDL rendering`.

---

### Task 5: Schema retrieval (embeddings + FK hop)

**Files:**
- Create: `backend/app/core/schema/retrieve.py`, `backend/tests/unit/test_retrieve.py`

**Interfaces:**
```python
class SchemaRetriever:
    def __init__(self, graph: SchemaGraph, llm: LLMClient, *, top_k: int = 4, hops: int = 1): ...
    async def build_index(self) -> None          # embeds every table.summary(); stores np.ndarray
    async def retrieve(self, question: str) -> list[str]   # ordered table names, top_k + neighbours, deduped
```

- [ ] **Step 1: Tests** with `FakeLLM` whose embed returns: vectors such that `"orders"` summary is closest to question "how many orders"; assert result starts with `orders` and includes `customers` via FK hop; assert `len(result) <= top_k + neighbours`; assert `retrieve` before `build_index` raises `RuntimeError`.
- [ ] **Step 2: Fail → Implement** with numpy cosine; keyword boost: if a table name or any column name appears as a whole word in the lowercased question, add `+0.2` to its score (the D2 mitigation).
- [ ] **Step 3: Pass. Commit** `feat(schema): embedding retrieval with FK-hop expansion and keyword boost`.

---

### Task 6: SQL guard (sqlglot)

**Files:**
- Create: `backend/app/core/sql/guard.py`, `backend/tests/unit/test_guard.py`

**Interfaces:**
```python
class GuardError(DomainError): reason: str   # reason ∈ {"parse","multi_statement","not_select","write_cte","into","lock","forbidden_function","unknown_table","set_op_non_select"}
def guard_sql(sql: str, known_tables: set[str], *, max_rows: int) -> str  # returns possibly-modified SQL with LIMIT
```

- [ ] **Step 1: Tests — the biggest suite in the repo.** Parametrized rejections (assert `GuardError.reason`):
  - `"DELETE FROM orders"` → not_select
  - `"SELECT 1; DROP TABLE orders"` → multi_statement
  - `"WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x"` → write_cte
  - `"SELECT * INTO t FROM orders"` → into
  - `"SELECT * FROM orders FOR UPDATE"` → lock
  - `"SELECT pg_sleep(10)"`, `"SELECT pg_read_file('/etc/passwd')"`, `"SELECT set_config('a','b',false)"`, `"SELECT * FROM dblink('x','y') AS t(a int)"` → forbidden_function
  - `"SELECT * FROM secrets"` → unknown_table
  - `"SELECT 1 UNION ALL DELETE FROM orders"` → parse or not_select (either acceptable; assert raises)
  - `"COPY orders TO PROGRAM 'ls'"` → not_select/parse
  - `"SELECT * FROM orders -- ; DROP"` → **accepted** (comment, prove regex would've been wrong)
  - `"SELECT 'DROP TABLE orders' AS s FROM orders"` → accepted
  - Accepts: `"SELECT * FROM orders"` → output ends with `LIMIT 500`; `"SELECT * FROM orders LIMIT 10"` unchanged limit; `"SELECT * FROM orders LIMIT 9999"` → clamped to 500; CTE with selects only accepted; subquery in FROM with alias accepted; `UNION` of two selects accepted and limit applied to the outer.
- [ ] **Step 2: Fail → Implement.** `sqlglot.parse(sql, read="postgres")` → `len != 1` → multi_statement; `isinstance(expr, exp.Command)` or parse error → parse; unwrap `exp.With` checking each CTE's `this` is `exp.Select`/`exp.Union`; root must be `Select`/`Union` (recursively check union arms); walk tree: any `exp.DML`/`exp.DDL`/`exp.Insert/Update/Delete/Merge` → write_cte or not_select; `expr.args.get("into")` → into; `exp.Lock` → lock; `exp.Anonymous`/`exp.Func` name in `FORBIDDEN = {"pg_sleep","pg_sleep_for","pg_sleep_until","pg_read_file","pg_read_binary_file","pg_ls_dir","pg_stat_file","dblink","dblink_exec","set_config","pg_terminate_backend","pg_cancel_backend","lo_import","lo_export","query_to_xml","current_setting"}` → forbidden_function; collect `exp.Table` names (excluding CTE aliases) not in `known_tables` → unknown_table; LIMIT: find outer `Limit`; none → `expr.limit(max_rows)`; present and > max → replace. Return `expr.sql(dialect="postgres")`.
- [ ] **Step 3: Pass. Commit** `feat(sql): sqlglot AST guard with LIMIT injection`.

---

### Task 7: Read-only executor

**Files:**
- Create: `backend/app/core/sql/executor.py`, `backend/app/db/session.py`, `backend/tests/integration/test_executor.py`

**Interfaces:**
```python
@dataclass class QueryResult: columns: list[str]; rows: list[list[Any]]; row_count: int; duration_ms: float; truncated: bool
class ExecutionError(DomainError): pg_message: str; kind: Literal["syntax","timeout","permission","other"]
async def execute_readonly(engine: AsyncEngine, sql: str, *, statement_timeout_ms: int, max_rows: int) -> QueryResult
# db/session.py
def make_engine(url: str, **kw) -> AsyncEngine
```

- [ ] **Step 1: Tests:** selects rows from `customers`; `max_rows` applied with `truncated=True` when more; `"SELECT pg_sleep(10)"` raised as `ExecutionError(kind="timeout")` with `statement_timeout_ms=200` (guard is bypassed here on purpose — executor must defend alone); `"INSERT ..."` → `kind="permission"`; bad syntax → `kind="syntax"`; JSON-serialisable values (Decimal→float, datetime→isoformat, UUID→str) — test `json.dumps(result.rows)` works.
- [ ] **Step 2: Fail → Implement:** `async with engine.connect() as c: await c.execute(text("SET LOCAL statement_timeout = :t")...)`; `SET TRANSACTION READ ONLY`; `result = await c.execute(text(sql))`; `fetchmany(max_rows + 1)`; map asyncpg errors by SQLSTATE (`57014` timeout, `42601` syntax, `25006`/`42501` permission).
- [ ] **Step 3: Pass. Commit** `feat(sql): read-only executor with timeout and row cap`.

---

### Task 8: Prompt builder + templates + output parsing

**Files:**
- Create: `backend/app/core/prompting/templates/generate.j2`, `repair.j2`, `backend/app/core/prompting/builder.py`, `backend/tests/unit/test_builder.py`

**Interfaces:**
```python
@dataclass class Prompt: system: str; user: str
class PromptBuilder:
    def __init__(self, version: str = "v1", examples: list[dict] | None = None): ...
    def generate(self, question: str, ddl: str) -> Prompt
    def repair(self, question: str, ddl: str, bad_sql: str, error: str) -> Prompt
def parse_llm_output(text: str) -> tuple[str, str]   # (sql, explanation); handles ```sql fences, JSON {"sql":..,"explanation":..}, or bare SQL
```

- [ ] **Step 1: Tests:** `generate` output contains the DDL, the question, "Postgres", "only SELECT", and ≤3 few-shot examples; `parse_llm_output` on fenced, JSON, bare, and "Here is the SQL:\n```sql\nSELECT 1\n```" all yield `SELECT 1`; trailing semicolon stripped.
- [ ] **Step 2: Fail → Implement.** System prompt: expert Postgres analyst, rules (SELECT only, use only given tables, `order_items.unit_price` for revenue, prefer explicit JOINs, always alias), output format: JSON `{"sql": "...", "explanation": "..."}`. Examples loaded from `seed/questions.yaml` by the caller (pipeline) — builder only accepts a list.
- [ ] **Step 3: Pass. Commit** `feat(prompting): versioned jinja templates and robust output parser`.

---

### Task 9: Pipeline orchestration

**Files:**
- Create: `backend/app/core/pipeline.py`, `backend/tests/integration/test_pipeline.py`

**Interfaces:**
```python
@dataclass class StageTiming: stage: str; ms: float
@dataclass class PipelineOutput:
    sql: str; explanation: str; result: QueryResult | None; tables: list[str]
    repaired: bool; usage: Usage; timings: list[StageTiming]; error: str | None
class Text2SQLPipeline:
    def __init__(self, *, llm: LLMClient, retriever: SchemaRetriever, graph: SchemaGraph,
                 builder: PromptBuilder, target_engine: AsyncEngine, settings: Settings): ...
    async def run(self, question: str) -> PipelineOutput
```

- [ ] **Step 1: Tests with `FakeLLM` + seeded container:**
  - happy path: FakeLLM returns valid JSON with `SELECT count(*) AS n FROM orders` → `result.rows == [[20000]]`, `repaired False`.
  - repair path: first response references `ordrs`, second is correct → `repaired True`, success.
  - guard rejection: FakeLLM returns `DELETE FROM orders` → `error` starts with `"guard:not_select"`, `result is None`, no second LLM call (assert FakeLLM call count == 1).
  - repair exhausted: two bad responses → `error` starts with `"execution:"`.
- [ ] **Step 2: Fail → Implement** stages `retrieve → render → generate → parse → guard → execute`, on `ExecutionError` of kind syntax/other do `repair` once; `GuardError` is terminal (don't ask the LLM to "fix" a DELETE — interview point). Accumulate usage and timings. Never raise — always return `PipelineOutput` with `error`.
- [ ] **Step 3: Pass. Commit** `feat(core): end-to-end pipeline with single repair attempt`.

---

### Task 10: App DB — QueryLog model + Alembic migration

**Files:**
- Create: `backend/app/db/models.py`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/0001_query_log.py`, `backend/tests/integration/test_query_log.py`

**Interfaces:**
```python
class QueryLog(Base): id UUID pk; created_at; question: str; sql: str | None; tables: list[str] (JSONB); success: bool; error: str | None; repaired: bool; latency_ms: float; prompt_tokens: int; completion_tokens: int; model: str; schema_version: str; request_id: str
async def record_query(session: AsyncSession, out: PipelineOutput, *, question, model, schema_version, request_id) -> QueryLog
```

- [ ] **Step 1: Test:** run Alembic `upgrade head` programmatically against the `app` DB from the container, insert via `record_query`, select back.
- [ ] **Step 2: Fail → Implement.** `env.py` reads `APP_DB_URL` (sync form: swap `+asyncpg` → `+psycopg`).
- [ ] **Step 3: Pass. Commit** `feat(db): query log table and alembic migration`.

---

### Task 11: FastAPI app — routes, DI, logging, lifespan

**Files:**
- Create: `backend/app/observability/logging.py`, `backend/app/api/deps.py`, `backend/app/api/v1/query.py`, `schema.py`, `health.py`, `backend/app/api/router.py`, `backend/app/main.py`, `backend/tests/integration/test_api.py`

**Interfaces:**
- `POST /api/v1/query {question}` → `QueryResponse {sql, explanation, columns, rows, row_count, truncated, tables, repaired, timings, usage, error, request_id}`; HTTP 200 even on pipeline error (error in body), 422 on empty question, 503 if schema not loaded.
- `GET /api/v1/schema` → `{version, tables: [{name, comment, columns:[...]}]}`; `POST /api/v1/schema/refresh` → re-introspect + re-index, returns new version.
- `GET /healthz` (always 200), `GET /readyz` (200 only after schema loaded and DBs reachable).
- Lifespan: configure structlog, create engines, introspect, build retriever index, build pipeline; store on `app.state`. `X-Request-ID` middleware binding into structlog contextvars.

- [ ] **Step 1: Tests** with `httpx.AsyncClient(transport=ASGITransport(app))`, settings overridden to container URLs and `app.state.llm = FakeLLM`. Cover all four endpoints + a 422 + that a row appears in `query_log` after a query.
- [ ] **Step 2: Fail → Implement.** `create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI` factory so tests inject fakes.
- [ ] **Step 3: Pass. Also run `uv run ruff check . && uv run mypy app/core`** and fix. Commit `feat(api): fastapi app with query, schema, health endpoints`.

---

### Task 12: Docker Compose + backend Dockerfile + Ollama profile

**Files:**
- Create: `backend/Dockerfile`, `docker-compose.yml`, `docker-compose.override.yml`, `.env.example`, `backend/entrypoint.sh`

- [ ] **Step 1: `backend/Dockerfile`** multi-stage: `uv` builder → `python:3.12-slim` runtime, non-root `app` user, `HEALTHCHECK CMD curl -f http://localhost:8000/healthz`, entrypoint runs `alembic upgrade head` then `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- [ ] **Step 2: `docker-compose.yml`** services: `postgres` (postgres:16-alpine, `./seed:/seed:ro` + `./seed/init.sh:/docker-entrypoint-initdb.d/10-init.sh:ro`, healthcheck `pg_isready`), `seed-data` build step: simplest robust approach — a `seed` build stage image whose `/seed/data` is copied into a named volume `seed_data` mounted into postgres at `/seed/data`; `backend` (depends_on postgres healthy; env from `.env`), `frontend` (Task 13), `ollama` under `profiles: ["ollama"]` with volume + an `ollama-pull` one-shot that runs `ollama pull $LLM_MODEL && ollama pull $EMBED_MODEL`. Override file: bind-mount `backend/app` with `--reload`, expose 5432.
- [ ] **Step 3: `.env.example`** with both an Ollama block and a commented OpenAI block (`LLM_PROVIDER=openai LLM_BASE_URL=https://api.openai.com LLM_MODEL=gpt-4o-mini EMBED_MODEL=text-embedding-3-small LLM_API_KEY=...`).
- [ ] **Step 4: Verify:** `docker compose --profile ollama up --build -d`, wait, `curl -X POST localhost:8000/api/v1/query -d '{"question":"how many orders are there"}' -H 'content-type: application/json'` returns SQL and `[[20000]]`. If Ollama is too slow on this machine, verify with `LLM_PROVIDER=openai` against any key and note it. Commit `build: docker compose stack with seeded postgres and optional ollama`.

---

### Task 13: Frontend (Vite + React + TS)

**Files:**
- Create: `frontend/package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`, `src/App.tsx`, `src/api.ts`, `src/components/{QuestionForm,SqlPanel,ResultsTable,SchemaSidebar}.tsx`, `src/App.css`, `src/api.test.ts`, `frontend/Dockerfile`, `frontend/nginx.conf`

- [ ] **Step 1:** `npm create vite@latest frontend -- --template react-ts`; add `vitest`. `api.ts`: typed `askQuestion(question): Promise<QueryResponse>` and `getSchema()`, base URL from `import.meta.env.VITE_API_URL ?? "/api"`. One vitest test mocking `fetch` for `askQuestion`.
- [ ] **Step 2:** UI: textarea + submit (Ctrl+Enter), loading state, SQL `<pre>`, explanation, results table (first 500 rows, column headers), error banner, timing/tokens footer, collapsible schema sidebar listing tables. Plain CSS, dark-ish neutral.
- [ ] **Step 3:** `Dockerfile`: `node:22-alpine` build → `nginx:alpine` serving `dist` with `nginx.conf` proxying `/api/` → `http://backend:8000/api/`. Add `frontend` service to compose on port 5173. Dev: `npm run dev` with Vite proxy.
- [ ] **Step 4:** `npm run build && npx tsc --noEmit && npx vitest run` pass; `docker compose up --build` → browser works end to end. Commit `feat(frontend): minimal react ui`.

---

### Task 14: Eval script, README, demo script, tag v0.1

**Files:**
- Create: `scripts/eval.py`, `README.md`, `docs/demo.md`, `docs/architecture.md`, `docs/decisions/0001-sql-safety.md`, `docs/decisions/0002-schema-retrieval.md`

- [ ] **Step 1: `scripts/eval.py`:** for each `questions.yaml` entry POST to `API_URL` (default `http://localhost:8000`), compare `row_count` to `expect_rows` (when present) and execution success; print a table and an overall `% executable` and `% row-count match`; write `eval-results.json`. No asserts — reporting only (D10).
- [ ] **Step 2: README** — pitch, architecture mermaid diagram, quickstart (`cp .env.example .env && make up`), switching LLM providers, running tests (`make test` needs Docker), first eval numbers from a real run, roadmap link to PLAN.md.
- [ ] **Step 3: `docs/demo.md`** — 2-minute script: up, browser question, show SQL, show a blocked `delete` attempt returning `guard:not_select`, `make logs` showing JSON logs.
- [ ] **Step 4: ADRs 0001 and 0002** from PLAN.md D1 and D2 in context/decision/consequences form.
- [ ] **Step 5:** `make lint && make test` green; `git tag v0.1`; commit `docs: readme, demo script, adrs; eval script`.

---

## Self-review

- **Spec coverage:** PLAN.md Phase 1 items 1–13 map to Tasks 12, 3, 4, 5, 8, 6, 7, 9, 11, 10, 13, 2+6+9+11 tests, 14. ✔
- **Placeholders:** Task 2 schema DDL and Task 13 component bodies are described rather than printed in full (they're long and fully specified by PLAN.md §2 / the component list); implementer writes them against the listed constraints.
- **Type consistency:** `Usage`, `Completion`, `SchemaGraph`, `SchemaRetriever`, `guard_sql`, `execute_readonly`, `QueryResult`, `PipelineOutput`, `PromptBuilder` names match across Tasks 3–11. ✔
