from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_db_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    target_db_url: str = "postgresql+asyncpg://readonly:readonly@localhost:5432/target"

    llm_provider: Literal["ollama", "openai"] = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = "unused"
    llm_model: str = "qwen2.5-coder:7b"
    embed_model: str = "nomic-embed-text"
    llm_timeout_s: float = 60.0
    #: Cap on how long `create_app()`'s lifespan will wait for the initial
    #: `SchemaRetriever.build_index()` embedding call before giving up and
    #: continuing startup anyway (see `app.main`'s lifespan) -- deliberately
    #: shorter than `llm_timeout_s` so a slow-but-reachable LLM can't stall
    #: startup past the Docker `HEALTHCHECK`'s `--start-period`.
    startup_embed_timeout_s: float = 20.0

    retrieve_top_k: int = 4
    max_rows: int = 500
    statement_timeout_ms: int = 5000
    prompt_version: str = "v1"
    cors_origins: list[str] = ["http://localhost:5173"]

    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = True
    sql_cache_ttl_s: int = 86400
    result_cache_ttl_s: int = 300
    schema_cache_ttl_s: int = 604800
    #: A serialised result-cache entry (sql + explanation + tables + a full
    #: `QueryResult`, JSON-encoded) larger than this is never stored -- see
    #: `QueryCache.set_result`. Guards against a single huge row set evicting
    #: everything else out of Redis under an LRU/memory-pressure policy.
    result_cache_max_bytes: int = 1_048_576

    #: Whether to record Prometheus metrics and mount `GET /metrics` --
    #: see `app.observability.metrics.Metrics`/`MetricsObserver` and
    #: `app.main.create_app`. `False` skips instrumenting HTTP requests and
    #: falls back to whatever `observer=`/`NullObserver` the pipeline would
    #: otherwise use, with no `/metrics` endpoint mounted at all.
    metrics_enabled: bool = True
    #: `{model: (usd_per_1k_prompt_tokens, usd_per_1k_completion_tokens)}`,
    #: used by `MetricsObserver.on_llm` to compute `t2s_llm_cost_usd_total`.
    #: A model absent from this table (e.g. any local Ollama model) costs
    #: `0` -- there is no per-token price to look up.
    llm_prices_usd_per_1k: dict[str, tuple[float, float]] = {
        "gpt-4o-mini": (0.00015, 0.0006),
        "gpt-4o": (0.0025, 0.01),
        "gpt-4.1-mini": (0.0004, 0.0016),
    }

    #: Max concurrent jobs the arq worker (`app.jobs.worker.WorkerSettings`)
    #: runs at once -- same knob as `Worker(max_jobs=...)`.
    arq_max_jobs: int = 4
    #: How often (seconds) the API lifespan's background sampler reads the
    #: arq queue depth (`ZCARD arq:queue`) into `t2s_jobs_queue_depth` -- see
    #: `app.main`'s queue-depth loop.
    queue_depth_sample_s: float = 5.0
    #: Port the arq worker serves its own Prometheus registry on (see
    #: `app.observability.exporter` and `app.jobs.worker`); scraped as the
    #: `text2sql-worker` job in `deploy/prometheus/prometheus.yml`. `0`
    #: disables the worker exporter. Ignored when `metrics_enabled` is
    #: false, and ignored entirely in the API process, which serves its
    #: registry from `GET /metrics` instead.
    worker_metrics_port: int = 9100

    log_level: str = "INFO"
    #: Path to the few-shot examples YAML, relative to the CWD the app is
    #: launched from (`backend/` locally; in the Docker image `seed/` is
    #: copied to `/app/seed`, so this is overridden there). Missing file is
    #: tolerated -- see `app.main._load_examples`.
    examples_path: str = "../seed/questions.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()
